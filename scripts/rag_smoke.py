"""导入公开产品样本并调用真实千问向量接口验证检索证据。"""

import hashlib
import json
import time
from pathlib import Path

from backend.core.config import Settings
from backend.db.store import Store, now
from backend.llm.qwen.embedding import QwenEmbedding
from backend.schemas import ProductInput
from backend.services.knowledge.ingestion import build_index, parse_isolated
from backend.services.knowledge.retrieval import Knowledge


def main():
    """向真实向量接口建立样本索引并验证九个问题的前四条证据。"""
    report_path = Path("docs/rag实测数据.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env()
    store = Store(settings.database)
    products = json.loads(Path("samples/products.json").read_text(encoding="utf-8"))
    queries = {
        "linear": [
            ("基础套餐按月付款多少钱？", "年付折算"),
            ("免费版最多能建多少团队和问题？", "250"),
            ("企业版多少钱？", "金额未公开"),
        ],
        "cal": [
            ("小团队怎么把预约轮流分给成员？", "轮转"),
            ("哪个套餐支持单点登录？", "SAML"),
            ("团队版一年一付每人每月多少钱？", "12"),
        ],
        "tally": [
            ("Pro 月付和年付分别多少钱？", "290"),
            ("表单数据想在过期后自动删除，需要哪个版本？", "保留期限"),
            ("免费版能不能收集不限次数的表单？", "公平使用"),
        ],
    }
    report = {
        "tested_at": now(),
        "model": settings.embedding_model,
        "dimension": settings.embedding_dimension,
        "chunk_budget": settings.chunk_tokens,
        "rrf_k": settings.rrf_k,
        "documents": [],
        "queries": [],
    }
    for sample in products:
        name = f"{sample['name']} · 官网资料样本"
        product = next((p for p in store.products() if p["name"] == name), None)
        if not product:
            product = store.save_product(
                ProductInput(
                    name=name,
                    audience=sample["audience"],
                    features=sample["category"],
                    pricing="报价请检索本产品资料；必须说明币种和计费周期。",
                    limitations=sample["limits"] + "本演示不代表厂商官方销售或授权代理。",
                    opening=f"您好，我是 AI 产品顾问，可以依据公开资料介绍 {sample['name']}。您目前有什么需求？",
                ).model_dump()
            )
        pid = product["id"]
        filename = f"{sample['name'].replace('.', '_')}_产品资料.pdf"
        doc = next((d for d in store.documents(pid) if d["name"] == filename and d.get("rag")), None)
        embedder = QwenEmbedding(settings, on_usage=lambda usage, pid=pid: store.log_embedding(pid, usage))
        started = time.perf_counter()
        data = (Path("output/pdf") / filename).read_bytes()
        if doc is None or doc["rag"].get("file_sha256") != hashlib.sha256(data).hexdigest():
            parsed = parse_isolated(filename, data, settings)
            rag = build_index(parsed, settings, embedder)
            if doc:
                store.update_index(pid, doc["id"], rag)
                doc = {**doc, "text": parsed["text"], "rag": rag}
            else:
                doc = store.add_document(pid, filename, parsed["text"], rag)
        report["documents"].append(
            {
                "name": filename,
                "product_id": pid,
                "chunks": len(doc["rag"]["chunks"]),
                "tables": doc["rag"].get("table_count", 0),
                "max_tokens": max(c["tokens"] for c in doc["rag"]["chunks"]),
                "index_or_load_ms": round((time.perf_counter() - started) * 1000),
                "usage": doc["rag"]["usage"],
                "warnings": doc["rag"]["warnings"],
            }
        )
        kb = Knowledge(store.documents(pid), settings, embedder)
        for query, expected in queries[sample["slug"]]:
            started = time.perf_counter()
            matches = kb.search(query)
            report["queries"].append(
                {
                    "product": sample["name"],
                    "query": query,
                    "expected_evidence": expected,
                    "evidence_in_top4": any(expected in m["text"] for m in matches),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "matches": matches,
                    "usage": kb.last_usage,
                }
            )
        print(f"{sample['name']}: {len(doc['rag']['chunks'])} chunks; hybrid search completed")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(q["evidence_in_top4"] for q in report["queries"])
    print(f"Evidence coverage: {passed}/{len(report['queries'])}; report: docs/rag实测数据.json")
    if passed != len(report["queries"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
