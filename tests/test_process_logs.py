"""通过真实子进程验证中文输出、编码边界及追加日志不会丢失。"""

import sys

from scripts.process_logs import LoggedProcess


def test_utf8_streams_survive_split_bytes_and_no_final_newline(tmp_path):
    """输出分块、缺失末尾换行与非法字节时仍生成可严格解码的 UTF-8 文件。

    Args:
        tmp_path: 隔离日志目录。
    """
    output = "中文日志 🎤".encode("utf-8")
    error = "错误输出".encode("utf-8")
    code = (
        "import os,sys,time; assert sys.stdout.encoding.lower()=='utf-8'; "
        f"data=bytes.fromhex('{output.hex()}'); "
        "os.write(1,data[:1]); time.sleep(0.1); os.write(1,data[1:]); "
        f"os.write(2,bytes.fromhex('{error.hex()}')+bytes([255]))"
    )
    capture = LoggedProcess([sys.executable, "-c", code], tmp_path, "test", log_dir=tmp_path,
                            env={"PYTHONIOENCODING": "ascii"})
    assert capture.process.wait(timeout=15) == 0
    capture.drain()
    assert (tmp_path / "test.stdout.log").read_text(encoding="utf-8") == "中文日志 🎤\n"
    assert (tmp_path / "test.stderr.log").read_text(encoding="utf-8") == "错误输出\\xff\n"
    assert all(not thread.is_alive() for thread in capture.threads)


def test_restarts_append_and_keep_real_exit_code(tmp_path):
    """重启追加历史日志，且子进程失败不会被包装成成功。

    Args:
        tmp_path: 隔离日志目录。
    """
    for message, code in (("第一次启动", 0), ("第二次启动", 7)):
        command = [sys.executable, "-c", f"import sys; print({message!r}); sys.exit({code})"]
        capture = LoggedProcess(command, tmp_path, "restart", log_dir=tmp_path)
        assert capture.process.wait(timeout=15) == code
        capture.drain()
    assert (tmp_path / "restart.stdout.log").read_text(encoding="utf-8").splitlines() == ["第一次启动", "第二次启动"]
