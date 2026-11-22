"""计算可观测响应延迟的分位数和语音模型费用估算。"""

import math


def percentiles(values):
    """过滤无效观测值并按最近秩计算 P50 和 P95。

    Args:
        values: 可包含空值的延迟观测序列，单位由调用方统一。

    Returns:
        有效样本数及分位数；无样本时分位数为空。
    """
    values = sorted(v for v in values if isinstance(v, (float, int)) and math.isfinite(v) and v >= 0)
    return {
        "count": len(values),
        "p50": round(values[math.ceil(len(values) * 0.5) - 1], 1) if values else None,
        "p95": round(values[math.ceil(len(values) * 0.95) - 1], 1) if values else None,
    }


def estimate_cost(usages, model, region):
    """按代码中的地域单价和已返回的模态用量估算语音费用。

    Args:
        usages: 模型响应提供的 usage 列表。
        model: 模型名称，决定已配置的计价表。
        region: 计价地域；当前只处理北京。

    Returns:
        人民币估算金额；模型、地域或模态明细不足时为空，不含 Embedding 费用。
    """
    rates = {
        "qwen-audio-3.1-realtime-plus": (5, 40, 40, 150),
        "qwen-audio-3.1-realtime-flash": (1.5, 6, 4.5, 12),
    }
    if model not in rates or region != "cn-beijing":
        return None
    text_in, audio_in, text_out, audio_out = rates[model]
    total = 0.0
    for usage in usages:
        inp, out = usage.get("input_tokens_details"), usage.get("output_tokens_details")
        if inp is None or out is None:
            return None  # Never invent a modality split from total_tokens.
        total += inp.get("text_tokens", 0) * text_in + inp.get("audio_tokens", 0) * audio_in
        audio = out.get("audio_tokens", 0)
        total += audio * audio_out if audio else out.get("text_tokens", 0) * text_out
    return round(total / 1_000_000, 6)
