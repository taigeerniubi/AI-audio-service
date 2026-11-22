"""下载并校验千问分词器文件，不下载本地模型权重。"""

import hashlib
from pathlib import Path

import httpx

from backend.core.config import Settings


def main():
    """从官方源下载 tokenizer，校验有效后原子替换本地文件。"""
    target = Path(Settings.from_env().tokenizer_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    url = "https://modelscope.cn/models/Qwen/Qwen3-Embedding-0.6B/resolve/master/tokenizer.json"
    if not target.exists():
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
        from tokenizers import Tokenizer

        Tokenizer.from_str(response.text)  # Validate before atomically publishing.
        pending = target.with_suffix(".tmp")
        pending.write_bytes(response.content)
        pending.replace(target)
    print(f"Qwen tokenizer ready: {target}; SHA256={hashlib.sha256(target.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
