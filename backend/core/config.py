"""加载本地环境变量并校验千问模型和运行参数。"""

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

from backend.core.origin import normalize_public_origin
from backend.core.paths import PROJECT_ROOT
from backend.llm.qwen.voices import DEFAULT_VOICE, RETIRED_VOICES


@dataclass
class Settings:
    api_key: str = ""
    workspace: str = ""
    region: str = "cn-beijing"
    model: str = "qwen-audio-3.1-realtime-plus"
    voice: str = DEFAULT_VOICE
    turn_detection: str = "smart_turn_v2"
    vad_silence_ms: int = 500
    database: str = "data/voice.db"
    max_seconds: float = 600
    reminder_seconds: float = 8
    idle_close_seconds: float = 20
    embedding_model: str = "text-embedding-v4"
    embedding_dimension: int = 1024
    tokenizer_path: str = ".cache/tokenizers/qwen3/tokenizer.json"
    chunk_tokens: int = 512
    rrf_k: int = 60
    public_origin: str = ""

    def __post_init__(self):
        """校验公开来源，并将已移除的旧音色迁移到当前默认值。"""
        self.public_origin = normalize_public_origin(self.public_origin)
        if self.voice in RETIRED_VOICES:
            self.voice = DEFAULT_VOICE

    @classmethod
    def from_env(cls):
        """从项目环境文件和进程环境读取配置，已有环境变量优先。

        Returns:
            尚未进行模型权限验证的配置实例。
        """
        load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            workspace=os.getenv("DASHSCOPE_WORKSPACE_ID", ""),
            region=os.getenv("QWEN_REGION", "cn-beijing"),
            model=os.getenv("QWEN_MODEL", "qwen-audio-3.1-realtime-plus"),
            voice=os.getenv("QWEN_VOICE", DEFAULT_VOICE),
            turn_detection=os.getenv("QWEN_TURN_DETECTION", "smart_turn_v2"),
            vad_silence_ms=int(os.getenv("QWEN_VAD_SILENCE_MS", "500")),
            database=os.getenv("DATABASE_PATH", "data/voice.db"),
            max_seconds=float(os.getenv("SESSION_MAX_SECONDS", "600")),
            reminder_seconds=float(os.getenv("IDLE_REMINDER_SECONDS", "8")),
            idle_close_seconds=float(os.getenv("IDLE_CLOSE_SECONDS", "20")),
            embedding_model=os.getenv("QWEN_EMBEDDING_MODEL", "text-embedding-v4"),
            embedding_dimension=int(os.getenv("QWEN_EMBEDDING_DIMENSION", "1024")),
            tokenizer_path=os.getenv("QWEN_TOKENIZER_PATH", ".cache/tokenizers/qwen3/tokenizer.json"),
            chunk_tokens=int(os.getenv("RAG_CHUNK_TOKENS", "512")),
            rrf_k=int(os.getenv("RAG_RRF_K", "60")),
            public_origin=os.getenv("PUBLIC_ORIGIN", ""),
        )

    def turn_detection_config(self) -> dict:
        """校验并构造轮次配置，只向声学模式发送固定静音阈值。

        Returns:
            首次 session.update 使用的轮次检测参数。

        Raises:
            ValueError: 模式不支持或声学判停时间超出官方范围。
        """
        if self.turn_detection not in {"server_vad", "smart_turn_v2"}:
            raise ValueError("轮次检测仅支持 server_vad 或 smart_turn_v2")
        if not 200 <= self.vad_silence_ms <= 6000:
            raise ValueError("声学判停时间须为 200～6000 ms")
        result = {"type": self.turn_detection}
        if self.turn_detection == "server_vad":
            result["silence_duration_ms"] = self.vad_silence_ms
        return result

    def missing(self):
        """列出当前必填但未配置的凭证名称。

        Returns:
            缺失配置项列表；不包含凭证值。
        """
        return ["DASHSCOPE_API_KEY"] if not self.api_key.strip() else []

    def url(self):
        """校验地域、模型名和工作空间后构造实时接口地址。

        Returns:
            仅指向受支持千问服务域名的 WebSocket URL。

        Raises:
            ValueError: 地域不支持或模型、工作空间格式非法。
        """
        if self.workspace and not re.fullmatch(r"[A-Za-z0-9_-]+", self.workspace):
            raise ValueError("Workspace ID 格式不正确")
        if self.region not in {"cn-beijing", "ap-southeast-1"}:
            raise ValueError("不支持的地域")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.model):
            raise ValueError("模型名称格式不正确")
        host = (
            f"{self.workspace}.{self.region}.maas.aliyuncs.com"
            if self.workspace
            else "dashscope.aliyuncs.com"
            if self.region == "cn-beijing"
            else "dashscope-intl.aliyuncs.com"
        )
        return f"wss://{host}/api-ws/v1/realtime?model={self.model}"

    def embedding_url(self):
        """校验向量模型、维度和切片预算并构造向量接口地址。

        Returns:
            与实时模型采用相同地域和工作空间的 HTTPS URL。

        Raises:
            ValueError: 配置不在当前适配器支持范围内。
        """
        self.url()  # Reuse region/workspace validation; never accept arbitrary provider URLs.
        if self.embedding_model != "text-embedding-v4":
            raise ValueError("当前适配器仅验证了 text-embedding-v4；更换模型需同时核对上下文和批次限制")
        if self.embedding_dimension not in {64, 128, 256, 512, 768, 1024, 1536, 2048}:
            raise ValueError("Embedding 维度不受支持")
        if not 128 <= self.chunk_tokens <= 2048 or not 1 <= self.rrf_k <= 1000:
            raise ValueError("切片预算须为 128～2048 Token；RRF k 须为 1～1000")
        host = self.url().split("/")[2]
        return f"https://{host}/api/v1/services/embeddings/text-embedding/text-embedding"
