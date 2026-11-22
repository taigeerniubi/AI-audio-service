"""按预设官网地址采集产品页面并保存来源证据，不递归跟随链接。"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

URLS = [
    "https://linear.app/pricing",
    "https://cal.com/pricing",
    "https://tally.so/pricing",
    "https://tally.so/help/tally-pro",
    "https://tally.so/help/tally-business",
]
AGENT = "ProductResearchDemo/1.0"


def main():
    """逐站检查抓取许可并保存预设页面、文本摘要和采集清单。"""
    folder = Path("data/crawl")
    folder.mkdir(parents=True, exist_ok=True)
    manifest = []
    robots = {}
    with httpx.Client(timeout=25, headers={"User-Agent": AGENT}, follow_redirects=True) as client:
        for url in URLS:
            host = urlparse(url).netloc
            if host not in robots:
                response = client.get(f"https://{host}/robots.txt")
                parser = RobotFileParser()
                if response.status_code == 404:
                    parser.parse([])
                else:
                    response.raise_for_status()
                    parser.parse(response.text.splitlines())
                robots[host] = parser
            if not robots[host].can_fetch(AGENT, url):
                raise RuntimeError(f"robots.txt 不允许抓取 {url}")
            response = client.get(url)
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                raise RuntimeError("页面超过抓取大小限制")
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
            slug = host.replace(".", "_") + urlparse(url).path.replace("/", "_")
            (folder / f"{slug}.html").write_bytes(response.content)
            (folder / f"{slug}.txt").write_text(text, encoding="utf-8")
            manifest.append(
                {
                    "url": url,
                    "final_url": str(response.url),
                    "status": response.status_code,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                    "text_file": f"{slug}.txt",
                    "characters": len(text),
                }
            )
            print(f"Fetched {url}: {len(text)} characters")
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
