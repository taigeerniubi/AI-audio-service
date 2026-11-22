"""提供不暴露凭证的应用配置与健康检查接口。"""

from pathlib import Path

from fastapi import APIRouter, Depends

from backend.api.dependencies import Runtime, get_runtime
from backend.llm.qwen.voices import voice_options

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health(runtime: Runtime = Depends(get_runtime)):
    """返回模型配置状态及本地分词器是否就绪。

    Args:
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        脱敏配置与 tokenizer 就绪状态；不代表真实模型鉴权成功。
    """
    settings = runtime.settings
    return {
        "ok": True, "configured": not settings.missing(), "missing": settings.missing(),
        "model": settings.model, "region": settings.region, "voice": settings.voice,
        "voices": voice_options(settings.voice), "turn_detection": settings.turn_detection,
        "vad_silence_ms": settings.vad_silence_ms, "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "rag_tokenizer_ready": Path(settings.tokenizer_path).is_file(),
    }
