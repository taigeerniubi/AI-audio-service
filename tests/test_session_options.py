"""验证音色及判停参数按会话固定，非法选项不能触发模型连接。"""

from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.llm.qwen.voices import voice_options
from backend.main import create_app


def test_session_voice_and_turn_detection_are_isolated(tmp_path):
    """两个会话使用各自选项，修改全局默认值不影响已经创建的快照。

    Args:
        tmp_path: pytest 提供的隔离目录。
    """
    settings = Settings(api_key="fake", database=str(tmp_path / "sessions.db"))
    app = create_app(settings)
    with TestClient(app) as client:
        product = client.get("/api/products").json()[0]["id"]
        default = client.post("/api/sessions", json={"product_id": product})
        custom = client.post("/api/sessions", json={
            "product_id": product, "voice": "xunanchuan_v3.1", "turn_detection": "smart_turn_v2",
        })
        assert custom.status_code == 201
        settings.voice = "longanlingxin_v3.1"
        first = app.state.runtime.sessions.attach(default.json()["id"], None)
        second = app.state.runtime.sessions.attach(custom.json()["id"], None)
        first_config = first.context.session_update(first.snapshot["product"])["session"]
        second_config = second.context.session_update(second.snapshot["product"])["session"]
        assert first_config["voice"] == "longanhuan_v3.1"
        assert first_config["turn_detection"] == {"type": "smart_turn_v2"}
        assert second_config["voice"] == "xunanchuan_v3.1"
        assert second_config["turn_detection"] == {"type": "smart_turn_v2"}
        assert second.report()["voice"] == "xunanchuan_v3.1"


def test_invalid_voice_and_detection_are_rejected(tmp_path):
    """健康接口提供可选音色，未支持的音色或轮次模式不能创建会话。

    Args:
        tmp_path: pytest 提供的隔离目录。
    """
    app = create_app(Settings(api_key="fake", database=str(tmp_path / "options.db")))
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert "xunanchuan_v3.1" in {row["id"] for row in health.get("voices", [])}
        assert health["voice"] == "longanhuan_v3.1"
        assert "longanqian_v3.1" not in {row["id"] for row in health["voices"]}
        pid = client.get("/api/products").json()[0]["id"]
        assert client.post("/api/sessions", json={"product_id": pid, "voice": "wrong_model_voice"}).status_code == 422
        assert client.post("/api/sessions", json={"product_id": pid, "voice": "longanqian_v3.1"}).status_code == 422
        assert client.post("/api/sessions", json={"product_id": pid, "turn_detection": "unknown"}).status_code == 422
        assert client.get("/api/sessions").json() == []


def test_retired_voice_configuration_migrates(monkeypatch):
    """旧环境音色迁移为龙安欢，音色列表不能把旧值重新加入为自定义项。

    Args:
        monkeypatch: pytest 提供的环境变量隔离工具。
    """
    monkeypatch.setenv("QWEN_VOICE", "longanqian_v3.1")
    assert Settings.from_env().voice == "longanhuan_v3.1"
    assert Settings(voice="longanqian_v3.1").voice == "longanhuan_v3.1"
    assert "longanqian_v3.1" not in {row["id"] for row in voice_options("longanqian_v3.1")}
    assert {"id": "custom_voice", "name": "自定义音色（后端配置）"} in voice_options("custom_voice")
