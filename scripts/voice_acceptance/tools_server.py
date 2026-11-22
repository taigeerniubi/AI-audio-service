"""复用真实语音验收服务，将已有产品索引只读复制到独立的工具延迟测试库。"""

import json
import os
import sqlite3
from pathlib import Path

from fastapi.responses import Response

from backend.core.paths import FRONTEND_DIST
from scripts.voice_acceptance import server as bench


def copy_product() -> dict:
    """复制 Tally 产品及既有向量，不重新解析文档或调用入库 Embedding。

    Returns:
        测试库中的产品档案。

    Raises:
        ValueError: 未指定独立测试批次，或原库缺少所需产品和索引。
    """
    if not bench.RUN_NAME.startswith("tool-latency-"):
        raise ValueError("工具延迟测试须使用独立的 tool-latency- 批次")
    target = bench.app.state.store
    current = target.products()[0]
    existing_documents = target.documents(current["id"])
    if existing_documents and current["name"].startswith("Tally"):
        return current
    source = Path(os.getenv("ACCEPTANCE_SOURCE_DB", "data/voice.db")).resolve()
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        products = [json.loads(row[0]) for row in db.execute("SELECT body FROM products")]
        product = next((row for row in products if row["name"].startswith("Tally")), None)
        if not product:
            raise ValueError("源库没有 Tally 官网资料样本")
        documents = db.execute(
            "SELECT name, text, rag FROM documents WHERE product_id = ?", (product["id"],)
        ).fetchall()
    if not documents or any(not row[2] for row in documents):
        raise ValueError("源产品没有完整的既有检索索引")
    copied = target.save_product(product, current["id"])
    if not existing_documents:
        for name, text, rag in documents:
            target.add_document(copied["id"], name, text, json.loads(rag))
    return copied


product = copy_product()
app = bench.app


@app.get("/audio-worklet.js", include_in_schema=False)
async def measured_playback() -> Response:
    """仅在测试服务中加入有效声音探针，保持生产播放器输出样本不变。

    Returns:
        含首个幅度不低于 0.002 样本回调的播放器源码。

    Raises:
        ValueError: 播放器实现已变化，原探针插入位置失效。
    """
    source = (FRONTEND_DIST / "audio-worklet.js").read_text(encoding="utf-8")
    anchor = "      item.cursor += 24000 / sampleRate;"
    if source.count(anchor) != 1:
        raise ValueError("播放探针插入位置已变化")
    probe = """
      this.voicedResponses ??= new Set();
      if (Math.abs(output[i - 1]) >= 0.002 && !this.voicedResponses.has(item.response_id)) {
        this.voicedResponses.add(item.response_id);
        this.port.postMessage({ type: 'audible', response_id: item.response_id, epoch: item.epoch });
      }
"""
    return Response(
        source.replace(anchor, probe + anchor), media_type="application/javascript",
        headers={"Cache-Control": "no-store", "X-Acceptance-Playback-Probe": "1"},
    )


# 根静态挂载应最后匹配，保证 AudioWorklet 实际取得带探针的测试脚本。
for mount in [route for route in app.router.routes if getattr(route, "name", None) == "frontend"]:
    app.router.routes.remove(mount)
    app.router.routes.append(mount)
