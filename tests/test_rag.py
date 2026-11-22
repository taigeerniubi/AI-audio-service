"""离线验证向量接口、混合检索、文档切分和表格保真边界。"""
import io
import json
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.db.store import Store
from backend.llm.qwen.embedding import EmbeddingError, QwenEmbedding
from backend.services.knowledge.ingestion import build_index, parse_document, preflight
from backend.services.knowledge.retrieval import Knowledge, rrf
from backend.services.knowledge.tables import table_chunks


@pytest.fixture
def settings(tmp_path):
    # An offline byte-level BPE tokenizer tests exact boundary handling. Live
    # Qwen-tokenizer/model validation is separately recorded by rag_smoke.py.
    """创建纯离线分词器和隔离的向量测试配置。

    Args:
        tmp_path: pytest 为本测试分配的临时目录。

    Returns:
        使用 64 维向量和临时文件的 Settings。
    """
    from tokenizers import Tokenizer, models, pre_tokenizers

    vocab = {token: i for i, token in enumerate(pre_tokenizers.ByteLevel.alphabet())}
    tokenizer = Tokenizer(models.BPE(vocab=vocab, merges=[]))
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    from tokenizers.decoders import ByteLevel

    tokenizer.decoder = ByteLevel()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return Settings(
        api_key="fake-secret",
        database=str(tmp_path / "rag.db"),
        tokenizer_path=str(path),
        embedding_dimension=64,
    )


class FakeEmbedding:
    def embed(self, texts, kind="document"):
        """返回固定单位向量和假用量，不调用云接口。

        Args:
            texts: 待模拟向量化的文本。
            kind: 协议用途参数，本替身对两种用途返回相同向量。

        Returns:
            与输入等长的向量列表和假用量。
        """
        return [[1.0] + [0.0] * 63 for _ in texts], [{"usage": {"total_tokens": len(texts)}}]


def fake_parse(name, data, settings):
    """保留预检查并返回单片段结果，隔离上传测试与复杂解析器。

    Args:
        name: 测试资料文件名。
        data: 测试正文的字节内容。
        settings: 保留与真实解析器相同的配置参数。

    Returns:
        不含向量的最小解析结果。
    """
    preflight(name, data)
    text = data.decode()
    return {"text": text, "chunks": [{"source": name, "text": text, "tokens": 5}], "warnings": []}


def test_rrf_uses_one_based_ranks_and_missing_zero():
    """验证 RRF 排名从一开始，同通道去重且缺席通道贡献零。"""
    ranking, scores = rrf([["a", "b", "a"], ["b", "c"]], k=60)
    assert ranking[0] == "b"
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["c"] == pytest.approx(1 / 62)


def test_embedding_batches_orders_normalizes_and_records_usage(settings):
    """验证向量分批、返回排序、单位化和每批用量记录。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
    """
    calls, usage = [], []

    def handler(request):
        """记录请求并故意逆序返回向量，用于验证恢复输入顺序。

        Args:
            request: httpx MockTransport 收到的请求。

        Returns:
            带假向量和用量的 HTTP 200 响应。
        """
        body = json.loads(request.content)
        calls.append(body)
        rows = [{"text_index": i, "embedding": [2.0] * 64} for i in range(len(body["input"]["texts"]))]
        return httpx.Response(
            200,
            json={
                "output": {"embeddings": list(reversed(rows))},
                "usage": {"total_tokens": 10},
                "request_id": str(len(calls)),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        vectors, usages = QwenEmbedding(settings, client, usage.append).embed(["price"] * 21)
    assert [len(c["input"]["texts"]) for c in calls] == [10, 10, 1]
    assert len(vectors) == 21 and len(usages) == len(usage) == 3
    assert sum(v * v for v in vectors[0]) == pytest.approx(1)
    assert calls[0]["parameters"]["text_type"] == "document"


@pytest.mark.parametrize(
    "response",
    [
        {"output": {"embeddings": []}},
        {"output": {"embeddings": [{"text_index": 0, "embedding": [0] * 64}]}},
        {"output": {"embeddings": [{"text_index": 0, "embedding": [1] * 63}]}},
    ],
)
def test_embedding_bad_vectors_rejected(settings, response):
    """验证缺失、零向量及错误维度均被拒绝。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
        response: 参数化提供的异常模型返回体。
    """
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))) as client:
        with pytest.raises(EmbeddingError):
            QwenEmbedding(settings, client).embed(["price"])


def test_embedding_length_guard_before_network(settings):
    """验证超长输入在发出模型请求前被拒绝。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
    """
    def handler(request):
        """把任何越过输入检查的网络请求判为测试失败。

        Args:
            request: 本不应发出的向量请求。
        """
        pytest.fail("oversized input must not reach the model")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EmbeddingError):
            QwenEmbedding(settings, client).embed(["中" * 9000])


def test_hybrid_semantic_only_and_snapshot_keeps_vectors(settings):
    """验证仅向量命中可返回，删除资料后已有快照仍可检索。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
    """
    store = Store(settings.database)
    pid = store.products()[0]["id"]
    parsed = fake_parse("price.md", b"Annual plan costs USD 290", settings)
    rag = build_index(parsed, settings, FakeEmbedding())
    doc = store.add_document(pid, "price.md", parsed["text"], rag)
    sid = store.create_session(pid)
    store.delete_document(pid, doc["id"])
    snapshot = store.session(sid)["snapshot"]
    matches = Knowledge(snapshot["documents"], settings, FakeEmbedding()).search("年费多少钱")
    assert matches[0]["vector_rank"] == 1 and matches[0]["bm25_rank"] is None
    assert matches[0]["retrieval_mode"] == "hybrid_rrf"
    assert not Knowledge(store.documents(pid), settings, FakeEmbedding()).search("年费")
    settings.embedding_dimension = 128
    with pytest.raises(ValueError, match="过期"):
        Knowledge(snapshot["documents"], settings, FakeEmbedding())


def test_docling_context_budget_and_unicode_not_truncated(settings):
    """验证标题计入 Token 预算且中文正文不会静默截断。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
    """
    body = "价格含税，年度套餐为290美元。\n\n" * 80
    parsed = parse_document("产品.md", ("# Tally 年费\n\n" + body).encode(), settings)
    assert len(parsed["chunks"]) > 1
    assert all(c["tokens"] <= settings.chunk_tokens for c in parsed["chunks"])
    assert all("Tally" in c["text"] for c in parsed["chunks"])
    assert sum(c["text"].count("290") for c in parsed["chunks"]) == 80


def test_table_xml_repeats_headers_escapes_and_never_splits_cells(settings):
    """验证 XML 转义、重复表头和完整单元格边界。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
    """
    settings.chunk_tokens = 512
    rows = [{"index": 1, "cells": [{"col": 1, "text": "Plan"}, {"col": 2, "text": "USD/year"}]}]
    rows += [
        {"index": i + 2, "cells": [{"col": 1, "text": f"Plan <{i}>&"}, {"col": 2, "text": str(i * 100)}]}
        for i in range(20)
    ]
    chunks, xml = table_chunks(rows, "prices.xlsx", "t1", settings, pages=[2])
    assert len(chunks) > 1
    assert ET.fromstring(xml).tag == "table"
    found = []
    for c in chunks:
        root = ET.fromstring(c["text"])
        assert root.find("header_candidates/row/cell").text == "Plan"
        assert c["tokens"] <= 512 and c["pages"] == [2]
        found.extend(int(row.attrib["index"]) for row in root.findall("rows/row"))
    assert found == list(range(2, 22))
    rows[1]["cells"][0]["text"] = "very long cell " * 200
    with pytest.raises(ValueError):
        table_chunks(rows, "prices.xlsx", "t1", settings)


def test_spanning_cell_context_survives_table_chunk_boundary(settings):
    """验证跨行单元格的含义在后续表格片段中保留。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
    """
    rows = [{"index": 1, "cells": [{"col": 1, "text": "Product"}, {"col": 2, "text": "Price"}]}]
    rows.append({"index": 2, "cells": [{"col": 1, "text": "Tally", "rowspan": 12}, {"col": 2, "text": "29"}]})
    rows += [
        {"index": i, "cells": [{"col": 2, "text": "290 dollars annual subscription"}]} for i in range(3, 14)
    ]
    chunks, _ = table_chunks(rows, "prices.xlsx", "t1", settings)
    assert len(chunks) > 1
    for c in chunks[1:]:
        cell = ET.fromstring(c["text"]).find("continued_cells/cell")
        assert cell is not None and cell.text == "Tally" and cell.attrib["anchor_row"] == "2"


@pytest.mark.parametrize("extension", ["docx", "xlsx", "pdf"])
def test_real_format_table_routes_to_xml(settings, extension):
    """验证 Word、Excel 和 PDF 表格独立转 XML 且不重复进入正文。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
        extension: 待验证的 docx、xlsx 或 pdf 扩展名。
    """
    buffer = io.BytesIO()
    rows = [["Plan", "USD/year"], ["Pro", "290"], ["Business", "890"]]
    if extension == "docx":
        from docx import Document

        doc = Document()
        doc.add_heading("Tally pricing", level=1)
        table = doc.add_table(rows=3, cols=2)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                table.cell(i, j).text = value
        doc.save(buffer)
    elif extension == "xlsx":
        from openpyxl import Workbook

        book = Workbook()
        book.active.title = "Annual USD prices"
        for row in rows:
            book.active.append(row)
        book.save(buffer)
        book.close()
    else:
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

        table = Table(rows, colWidths=[160, 160])
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
        SimpleDocTemplate(buffer).build([Paragraph("Tally pricing", getSampleStyleSheet()["Title"]), table])
    parsed = parse_document(f"prices.{extension}", buffer.getvalue(), settings)
    assert parsed["table_count"] == 1
    tables = [c for c in parsed["chunks"] if c.get("kind") == "table_xml"]
    assert tables and any("290" in c["text"] for c in tables)
    assert all("290" not in c["text"] for c in parsed["chunks"] if c.get("kind") != "table_xml")
    if extension == "xlsx":
        assert "Annual USD prices" in tables[0]["text"]
    for c in tables:
        ET.fromstring(c["text"])


def test_upload_embedding_failure_is_atomic_and_search_no_silent_fallback(settings):
    """验证向量服务失败时上传不保存半成品且返回明确错误。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
    """
    from backend.app import create_app

    class Broken:
        def embed(self, *args, **kwargs):
            """模拟向量服务不可用，验证业务层不会保存部分索引。

            Args:
                *args: 向量调用的位置参数。
                **kwargs: 向量调用的关键字参数。
            """
            raise EmbeddingError("model unavailable")

    app = create_app(settings, parser=fake_parse, embedder=Broken())
    with TestClient(app) as client:
        pid = client.get("/api/products").json()[0]["id"]
        response = client.post(f"/api/products/{pid}/documents", files={"file": ("x.md", b"price")})
        assert response.status_code == 502
        assert client.get(f"/api/products/{pid}/documents").json() == []


def test_office_formula_and_archive_bomb_rejected(settings):
    """验证含公式 Excel 和解压后超限的 Office 文件被拒绝。

    Args:
        settings: 带离线 tokenizer 和假凭证的测试配置。
    """
    import zipfile

    from openpyxl import Workbook

    buffer = io.BytesIO()
    book = Workbook()
    book.active.append(["Price", "=100*12"])
    book.save(buffer)
    book.close()
    with pytest.raises(ValueError, match="公式"):
        parse_document("prices.xlsx", buffer.getvalue(), settings)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * (31 * 1024**2))
    with pytest.raises(ValueError, match="解压"):
        preflight("bomb.docx", buffer.getvalue())
