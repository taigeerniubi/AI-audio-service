"""通过本地 HTTP 服务验证连接复用、启动预热及不同会话的用量隔离。"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from fastapi.testclient import TestClient
from tokenizers import Tokenizer, models

from backend.core.config import Settings
from backend.llm.qwen.embedding import load_tokenizer
from backend.main import create_app


def test_embeddings_reuse_connection_without_sharing_usage(tmp_path, monkeypatch):
    """两次真实本地 HTTP 请求复用 TCP，启动不请求模型，用量分别归属。

    Args:
        tmp_path: pytest 提供的隔离目录。
        monkeypatch: 仅将云服务地址替换成本地协议服务。
    """
    ports = []

    class Provider(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            """返回一个有效的 64 维向量，并记录实际 TCP 客户端端口。"""
            self.rfile.read(int(self.headers["Content-Length"]))
            ports.append(self.client_address[1])
            body = json.dumps({"output": {"embeddings": [
                {"text_index": 0, "embedding": [1.0] + [0.0] * 63},
            ]}, "usage": {"total_tokens": 1}, "request_id": str(len(ports))}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            """抑制本地协议服务的请求日志。

            Args:
                format: 标准库提供的日志格式。
                *args: 标准库提供的格式参数。
            """

    tokenizer_path = tmp_path / "tokenizer.json"
    Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]")).save(str(tokenizer_path))
    settings = Settings(api_key="fake", database=str(tmp_path / "pool.db"),
                        tokenizer_path=str(tokenizer_path), embedding_dimension=64)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(Settings, "embedding_url", lambda self: f"http://127.0.0.1:{server.server_port}/embed")
    app = create_app(settings)
    try:
        with TestClient(app):
            assert ports == []  # 本地预热不消耗模型额度。
            before = load_tokenizer.cache_info()
            load_tokenizer(settings.tokenizer_path)
            assert load_tokenizer.cache_info().misses == before.misses
            first_usage, second_usage = [], []
            first = app.state.runtime.llm.create_embedding(first_usage.append)
            second = app.state.runtime.llm.create_embedding(second_usage.append)
            assert first.embed(["价格"], "query")[0][0][0] == 1.0
            assert second.embed(["功能"], "query")[0][0][0] == 1.0
            assert len(ports) == 2 and len(set(ports)) == 1
            assert [row["request_id"] for row in first_usage] == ["1"]
            assert [row["request_id"] for row in second_usage] == ["2"]
        assert first.client.is_closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
