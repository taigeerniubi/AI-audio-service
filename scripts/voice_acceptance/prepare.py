"""裁剪合成语音边缘静音并生成有精确时间边界的暂停与噪声样本。"""

import hashlib
import json
import wave

import numpy as np

from backend.core.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT / ".cache" / "voice-acceptance" / "fixtures"


def write_wave(name: str, audio: np.ndarray, text: str, kind: str = "speech") -> dict:
    """保存 16 kHz PCM16 测试音频及检测到的有效声音边界。

    Args:
        name: 不含扩展名的样本标识。
        audio: 单声道 PCM16 采样。
        text: 人工编写的预期文本。
        kind: 语音或合成噪声类别。

    Returns:
        包含时长、有效起止点和校验和的元数据。
    """
    path = ROOT / f"{name}.wav"
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(audio.astype("<i2").tobytes())
    active = np.flatnonzero(np.abs(audio.astype(np.int32)) >= 120)
    return {
        "name": name,
        "text": text,
        "kind": kind,
        "sample_rate": 16000,
        "duration_ms": len(audio) / 16,
        "active_start_ms": int(active[0]) / 16 if len(active) else 0,
        "active_end_ms": (int(active[-1]) + 1) / 16 if len(active) else 0,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main() -> None:
    """生成可重复测试的语料清单，保留句中静音而裁剪边缘静音。"""
    phrases = json.loads((ROOT / "phrases.json").read_text(encoding="utf-8-sig"))
    manifest, samples = {}, {}
    for name, text in phrases.items():
        with wave.open(str(ROOT / f"{name}.wav"), "rb") as source:
            assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 16000)
            audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").copy()
        active = np.flatnonzero(np.abs(audio.astype(np.int32)) >= 120)
        if not len(active):
            raise ValueError(f"样本没有有效语音：{name}")
        audio = audio[max(0, active[0] - 160) : active[-1] + 161]
        samples[name] = audio
        manifest[name] = write_wave(name, audio, text)
    for gap in (600, 1200):
        audio = np.concatenate(
            [samples["pause_prefix"], np.zeros(gap * 16, dtype=np.int16), samples["pause_suffix"]]
        )
        name = f"pause_{gap}"
        manifest[name] = write_wave(name, audio, f"我想先问一下［停顿 {gap}ms］你可以用普通话和我交流吗？")
        manifest[name]["pause_start_ms"] = len(samples["pause_prefix"]) / 16
        manifest[name]["pause_end_ms"] = len(samples["pause_prefix"]) / 16 + gap
    rng = np.random.default_rng(20261111)
    noise = np.zeros(24000, dtype=np.float64)
    for offset in range(0, len(noise) - 600, 1900):
        noise[offset : offset + 600] += rng.normal(0, 0.18, 600) * np.exp(-np.arange(600) / 70)
    manifest["clicks"] = write_wave(
        "clicks",
        np.clip(noise * 32767, -32768, 32767).astype(np.int16),
        "合成键盘式短脉冲，不是真实键盘录音",
        "synthetic_noise",
    )
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({name: round(row["duration_ms"]) for name, row in manifest.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
