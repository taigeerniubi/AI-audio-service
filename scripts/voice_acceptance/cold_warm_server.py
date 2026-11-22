"""在独立进程中观测首次及后续检索，沿用当前应用的预热、连接池和轮次配置。"""

import time

from scripts.voice_acceptance import tools_server
from scripts.voice_acceptance.cold_warm_probe import ColdWarmProbe

bench = tools_server.bench
if not bench.RUN_NAME.startswith(("tool-latency-coldwarm-", "tool-latency-optimized-")) or bench.response_ids:
    raise ValueError("分阶段对照须使用没有既往响应的独立 coldwarm 或 optimized 批次")
probe = ColdWarmProbe(bench.OUTPUT)
probe.metadata["application"] = {
    "startup_prewarm": True, "embedding_keepalive_seconds": 60,
    "turn_detection": bench.settings.turn_detection_config(), "voice": bench.settings.voice,
}
probe.install()
probe.flush()


class ProfiledTransport(bench.ObservedTransport):
    """在既有十响应上限内观测连接建立和每个音频帧的发送耗时。"""

    def __init__(self):
        """为每个独立会话创建原模型连接及音频帧计数器。"""
        super().__init__()
        self.frame_sequence = 0

    async def open(self):
        """计时真实模型连接建立，沿用原鉴权、超时和响应预算。"""
        started = time.perf_counter()
        try:
            await super().open()
        finally:
            probe.record("realtime_connect", started, connection=self.connection_id)

    async def send(self, event):
        """转发原事件并保存音频帧 RMS 和时序，不保存原始录音。

        Args:
            event: 应用实际发给千问的协议事件。
        """
        started = time.perf_counter()
        await super().send(event)
        if event.get("type") == "input_audio_buffer.append":
            self.frame_sequence += 1
            probe.audio_frame(self.connection_id, self.frame_sequence, event["audio"], started, time.perf_counter())

    async def close(self):
        """原连接关闭后保存脱敏观测，避免逐帧磁盘 I/O 干扰测量。"""
        try:
            await super().close()
        finally:
            probe.flush()


app = tools_server.app
app.state.runtime.llm.realtime_builder = ProfiledTransport
