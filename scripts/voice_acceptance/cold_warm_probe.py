"""为独立验收进程记录首次检索初始化、向量网络阶段及音频发送时间。"""

import base64
import contextvars
import hashlib
import itertools
import json
import math
import os
import struct
import time
from pathlib import Path

import httpx

from backend.llm import factory
from backend.llm.qwen import embedding
from backend.services.knowledge import retrieval


class ColdWarmProbe:
    """只在测试进程包装原实现，不预加载分词器、不复用额外 HTTP 连接。"""

    def __init__(self, output: Path):
        """创建内存记录器，采样阶段不逐帧写磁盘。

        Args:
            output: 已授权测试批次的本地产物目录。
        """
        self.output = output
        self.events = []
        self.audio_frames = []
        self.context = contextvars.ContextVar("cold_warm_phase", default={})
        self.knowledge_counter = itertools.count(1)
        self.query_counter = itertools.count(1)
        self.client_counter = itertools.count(1)
        self.metadata = {
            "pid": os.getpid(),
            "installed_at_monotonic": time.perf_counter(),
            "tokenizer_cache_at_install": embedding.load_tokenizer.cache_info()._asdict(),
            "http_connections_reused_by_probe": False,
        }

    def record(self, phase: str, started: float, **detail) -> None:
        """保存单调时钟阶段记录，调用方仅传入脱敏元数据。

        Args:
            phase: 阶段名称。
            started: 此阶段开始时的 perf_counter 值。
            **detail: 缓存状态、结果数量等不含凭证或原始音频的字段。
        """
        ended = time.perf_counter()
        self.events.append({
            "phase": phase, "started": started, "ended": ended,
            "elapsed_ms": round((ended - started) * 1000, 3),
            **self.context.get(), **detail,
        })

    def audio_frame(self, connection: int, sequence: int, audio: str, started: float, ended: float) -> None:
        """记录发出的有效音频边界和发送等待，不保存 PCM 或 Base64。

        Args:
            connection: 对应真实模型连接的编号。
            sequence: 此连接从一开始累计的 20 ms 音频帧号。
            audio: 当前帧 Base64，仅即时计算 RMS。
            started: 进入 WebSocket 发送前的单调时刻。
            ended: WebSocket 发送返回后的单调时刻。
        """
        pcm = base64.b64decode(audio)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
        self.audio_frames.append({
            "connection": connection, "sequence": sequence, "audio_end_ms": sequence * 20,
            "started": started, "ended": ended, "send_ms": round((ended - started) * 1000, 3),
            "rms": round(rms, 7), "active": rms >= 0.002,
        })

    def flush(self) -> None:
        """在会话结束后写出当前脱敏阶段和帧元数据。"""
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "cold-warm-probe.json").write_text(json.dumps({
            "metadata": self.metadata, "events": self.events, "audio_frames": self.audio_frames,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def install(self) -> None:
        """包装测试进程原有调用，保持生产计算、请求顺序及客户端生命周期。"""
        probe = self
        original_loader = embedding.load_tokenizer
        original_embed = embedding.QwenEmbedding.embed
        original_init = retrieval.Knowledge.__init__
        original_search = retrieval.Knowledge.search
        original_client = httpx.Client

        def load_tokenizer(path):
            """记录原分词器 LRU 缓存的加载或复用耗时。

            Args:
                path: 原实现提供的分词器文件路径。

            Returns:
                原加载器返回的同一分词器对象。
            """
            before = original_loader.cache_info()
            started = time.perf_counter()
            result = original_loader(path)
            probe.record("tokenizer_load", started, cache_before=before._asdict(),
                         cache_after=original_loader.cache_info()._asdict())
            return result

        def embed(model, texts, kind="document"):
            """完整计时原向量调用，包括输入校验、网络及返回解析。

            Args:
                model: 原 QwenEmbedding 实例。
                texts: 原查询文本列表。
                kind: 原接口的 query 或 document 用途。

            Returns:
                原接口返回的向量与用量。
            """
            started = time.perf_counter()
            try:
                return original_embed(model, texts, kind)
            finally:
                probe.record("embedding_total", started, text_type=kind, count=len(texts))

        def knowledge_init(knowledge, documents, settings=None, embedder=None):
            """计时每个会话的检索器构造，保留首次 jieba 初始化开销。

            Args:
                knowledge: 原 Knowledge 实例。
                documents: 固定版本的文档和向量。
                settings: 原应用配置。
                embedder: 原向量客户端。
            """
            knowledge._acceptance_index = next(probe.knowledge_counter)
            token = probe.context.set({"knowledge_index": knowledge._acceptance_index})
            started = time.perf_counter()
            try:
                original_init(knowledge, documents, settings, embedder)
            finally:
                probe.record("knowledge_init", started)
                probe.context.reset(token)

        def search(knowledge, query):
            """计时原检索并为其嵌套的分词器和 HTTP 阶段关联同一查询。

            Args:
                knowledge: 原 Knowledge 实例。
                query: 模型实际生成的检索词。

            Returns:
                未修改的原检索结果。
            """
            token = probe.context.set({
                "knowledge_index": knowledge._acceptance_index,
                "query_index": next(probe.query_counter),
                "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                "query_chars": len(query),
            })
            started = time.perf_counter()
            result = None
            try:
                result = original_search(knowledge, query)
                return result
            finally:
                probe.record("knowledge_search", started, matches=len(result) if result is not None else None)
                probe.context.reset(token)

        class MeasuredClient(original_client):
            """沿用生产代码的连接池与生命周期，仅增添 HTTP 阶段计时。"""

            def __init__(self, *args, **kwargs):
                """计时 HTTP 客户端和 SSL 配置构造。

                Args:
                    *args: 原客户端的位置参数。
                    **kwargs: 原超时和客户端配置。
                """
                self.acceptance_client = next(probe.client_counter)
                started = time.perf_counter()
                super().__init__(*args, **kwargs)
                probe.record("http_client_init", started, client=self.acceptance_client)

            def post(self, *args, **kwargs):
                """记录真实 HTTP 往返，并由 httpcore 追踪 TCP、TLS 和响应等待。

                Args:
                    *args: 原请求位置参数，不写入日志。
                    **kwargs: 原请求体和鉴权头，不写入日志。

                Returns:
                    未修改的真实 HTTP 响应。
                """
                starts = {}

                def trace(event, info):
                    """仅记录 httpcore 阶段名，丢弃可能含凭证的事件详情。

                    Args:
                        event: httpcore 阶段及 started、complete、failed 后缀。
                        info: 原始事件详情，不读取、不持久化。
                    """
                    phase, _, status = event.rpartition(".")
                    if status == "started":
                        starts[phase] = time.perf_counter()
                    elif status in {"complete", "failed"} and phase in starts:
                        probe.record("http." + phase, starts.pop(phase), status=status, client=self.acceptance_client)

                kwargs["extensions"] = {**kwargs.get("extensions", {}), "trace": trace}
                started = time.perf_counter()
                status_code = None
                try:
                    response = super().post(*args, **kwargs)
                    status_code = response.status_code
                    return response
                finally:
                    probe.record("http_post", started, status_code=status_code, client=self.acceptance_client)

        # 这些替换只存在于专用验收进程；不修改生产源码，也不改变请求数据。
        embedding.load_tokenizer = load_tokenizer
        factory.load_tokenizer = load_tokenizer
        embedding.QwenEmbedding.embed = embed
        retrieval.Knowledge.__init__ = knowledge_init
        retrieval.Knowledge.search = search
        httpx.Client = MeasuredClient
