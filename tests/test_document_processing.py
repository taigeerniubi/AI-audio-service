"""验证上传阶段可观察、失败不入库，以及重试与产品之间的状态隔离。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from test_rag import FakeEmbedding, fake_parse
from test_rag import settings as rag_settings

from backend.llm.qwen.embedding import EmbeddingError
from backend.main import create_app

settings = rag_settings


def test_upload_exposes_real_stages_and_preserves_active_operation(settings):
    """阻塞真实解析与向量边界，检查阶段、原子提交、并发拒绝和产品隔离。

    Args:
        settings: 离线向量配置与临时数据库。
    """
    parsing, release_parse, embedding, release_embedding = (Event() for _ in range(4))

    def parser(name, data, config):
        """等待测试放行后返回切片。

        Args:
            name: 上传文件名。
            data: 文件内容。
            config: 本地解析配置。
        """
        parsing.set()
        assert release_parse.wait(10)
        return fake_parse(name, data, config)

    class SlowEmbedding(FakeEmbedding):
        def embed(self, texts, kind="document"):
            """等待测试放行后返回离线向量。

            Args:
                texts: 待向量化切片。
                kind: 向量用途。
            """
            embedding.set()
            assert release_embedding.wait(10)
            return super().embed(texts, kind)

    app = create_app(settings, parser=parser, embedder=SlowEmbedding())
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        pid = client.get("/api/products").json()[0]["id"]
        other = client.post("/api/products", json={"name": "另一产品"}).json()["id"]
        base = f"/api/products/{pid}"
        assert client.get(base + "/document-processing").json() is None
        future = pool.submit(client.post, base + "/documents?upload_id=attempt-1", files={"file": ("cpu.md", b"9700X 8 cores")})
        try:
            assert parsing.wait(10)
            state = client.get(base + "/document-processing").json()
            assert state["stage"] == "parsing" and state["id"] == "attempt-1"
            assert state["name"] == "cpu.md" and "finished_at" not in state
            assert client.get(base + "/documents").json() == []
            refused = client.post(base + "/documents?upload_id=attempt-2", files={"file": ("second.md", b"other")})
            assert refused.status_code == 429
            assert client.get(base + "/document-processing").json() == state
            assert client.get(f"/api/products/{other}/document-processing").json() is None
            release_parse.set()
            assert embedding.wait(10)
            state = client.get(base + "/document-processing").json()
            assert state["stage"] == "embedding" and state["chunks"] == 1
            assert client.get(base + "/documents").json() == []
        finally:
            release_parse.set()
            release_embedding.set()
        response = future.result(10)
        assert response.status_code == 201
        doc = response.json()
        state = client.get(base + "/document-processing").json()
        assert state["stage"] == "completed" and state["document"] == doc
        assert state["finished_at"] >= state["started_at"]
        assert "text" not in state["document"] and "rag" not in state["document"]
        assert client.get(base + "/documents").json() == [doc]
        client.delete(base + f"/documents/{doc['id']}")
        assert client.get(base + "/document-processing").json() is None


@pytest.mark.parametrize("failure", ["parse", "embedding"])
def test_upload_failure_status_and_retry_are_isolated(settings, failure):
    """失败显示真实原因，重试使用新 ID 并且只保存完整索引。

    Args:
        settings: 离线向量配置与临时数据库。
        failure: 本轮模拟失败的阶段。
    """
    broken = True

    def parser(name, data, config):
        """在指定用例中产生文件解析错误。

        Args:
            name: 上传文件名。
            data: 文件内容。
            config: 本地解析配置。
        """
        if broken and failure == "parse":
            raise ValueError("文件没有可提取的文字")
        return fake_parse(name, data, config)

    class RetryEmbedding(FakeEmbedding):
        def embed(self, texts, kind="document"):
            """在指定用例中产生向量服务错误。

            Args:
                texts: 待向量化切片。
                kind: 向量用途。
            """
            if broken and failure == "embedding":
                raise EmbeddingError("向量接口暂不可用")
            return super().embed(texts, kind)

    app = create_app(settings, parser=parser, embedder=RetryEmbedding())
    with TestClient(app) as client:
        pid = client.get("/api/products").json()[0]["id"]
        base = f"/api/products/{pid}"
        response = client.post(base + "/documents?upload_id=failed", files={"file": ("cpu.md", b"9700X")})
        assert response.status_code == (422 if failure == "parse" else 502)
        state = client.get(base + "/document-processing").json()
        assert state["id"] == "failed" and state["stage"] == "failed"
        assert state["error"] == response.json()["detail"]
        assert client.get(base + "/documents").json() == []
        broken = False
        response = client.post(base + "/documents?upload_id=retry", files={"file": ("cpu.md", b"9700X")})
        assert response.status_code == 201
        state = client.get(base + "/document-processing").json()
        assert state["id"] == "retry" and state["stage"] == "completed" and "error" not in state
        response = client.post(base + "/documents?upload_id=duplicate", files={"file": ("cpu.md", b"9700X")})
        assert response.status_code == 409
        assert client.get(base + "/document-processing").json()["stage"] == "failed"
        assert len(client.get(base + "/documents").json()) == 1


def test_reindex_reports_completion_and_status_rejects_unknown_products(settings):
    """索引升级同样给出完成状态，查询接口拒绝不存在的产品和非法关联 ID。

    Args:
        settings: 离线向量配置与临时数据库。
    """
    app = create_app(settings, parser=fake_parse, embedder=FakeEmbedding())
    with TestClient(app) as client:
        pid = client.get("/api/products").json()[0]["id"]
        doc = app.state.store.add_document(pid, "legacy.md", "9700X 8 cores")
        base = f"/api/products/{pid}"
        response = client.post(base + f"/documents/{doc['id']}/index?upload_id=reindex")
        assert response.status_code == 200 and response.json()["index_status"] == "ready"
        state = client.get(base + "/document-processing").json()
        assert state["id"] == "reindex" and state["stage"] == "completed"
        assert state["document"]["id"] == doc["id"]
        assert client.get("/api/products/absent/document-processing").status_code == 404
        assert client.post(base + "/documents?upload_id=bad%20id", files={"file": ("cpu.md", b"9700X")}).status_code == 422
