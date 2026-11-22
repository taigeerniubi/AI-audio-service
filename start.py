"""检查本地依赖并同时启动前后端，退出时清理本脚本创建的全部进程。"""

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from scripts.process_logs import LOG_DIR, LoggedProcess, file_handler

ROOT = Path(__file__).resolve().parent
BACKEND_URL = "http://127.0.0.1:8000/api/health"
FRONTEND_URL = "http://127.0.0.1:5173"


def commands() -> list[tuple[str, list[str], Path]]:
    """检查运行依赖并构造无需 shell 的前后端命令。

    Returns:
        服务名称、参数列表及工作目录组成的列表。

    Raises:
        RuntimeError: 依赖未安装或固定端口已被占用。
    """
    venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = str(venv) if venv.is_file() else sys.executable
    result = subprocess.run(
        [python, "-c", "import sys, fastapi, uvicorn, sqlalchemy; assert sys.version_info >= (3, 11)"],
        capture_output=True, timeout=20, cwd=ROOT,
    )
    if result.returncode:
        raise RuntimeError('需要 Python 3.11+ 和后端依赖，请按 README 首次安装步骤配置 .venv。')
    node = shutil.which("node")
    vite = ROOT / "frontend/node_modules/vite/bin/vite.js"
    if not node or not vite.is_file():
        raise RuntimeError("请安装 Node.js，并在 frontend 目录运行 npm ci。")
    for port in (8000, 5173):
        with socket.socket() as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"端口 {port} 已占用，请先关闭之前的服务；本脚本不会终止其他程序。") from None
    return [
        ("后端", [python, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8000"], ROOT),
        ("前端", [node, str(vite), "--host", "127.0.0.1", "--port", "5173", "--strictPort", "--clearScreen", "false"], ROOT / "frontend"),
    ]


def wait_ready(url: str, children: list[tuple[str, subprocess.Popen]], timeout: float = 45) -> None:
    """等待真实 HTTP 就绪，并在任一子进程提前退出时报告失败。

    Args:
        url: 本地健康检查或页面地址。
        children: 已经启动且归本脚本所有的进程。
        timeout: 最大等待秒数。

    Raises:
        RuntimeError: 服务退出或超过就绪等待时间。
    """
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, process in children:
            if process.poll() is not None:
                raise RuntimeError(f"{name}启动失败（退出码 {process.returncode}），请查看上方日志。")
        try:
            with opener.open(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(0.15)
    raise RuntimeError(f"服务未在 {timeout:g} 秒内就绪，请查看上方日志。")


def stop_children(children: list[tuple[str, subprocess.Popen]]) -> None:
    """先请求进程组退出，再清理超时进程树，绝不按名称批量结束程序。

    Args:
        children: 仅含本脚本创建的子进程。
    """
    for _, process in reversed(children):
        if process.poll() is not None:
            continue
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)


def interrupted(signum, frame) -> None:
    """将系统退出信号转为统一的清理路径。

    Args:
        signum: 收到的信号编号。
        frame: Python 提供的当前执行帧。
    """
    raise KeyboardInterrupt


def main() -> int:
    """启动两个服务并监控存活状态，用户退出或启动失败均清理进程。

    Returns:
        正常退出返回 0；依赖、端口或服务运行失败返回 1。
    """
    children = []
    captures = []
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    handler = file_handler(LOG_DIR / "startup.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("audio.startup")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    console = logging.StreamHandler()
    logger.handlers = [handler, console]
    signal.signal(signal.SIGTERM, interrupted)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, interrupted)
    try:
        specs = commands()
        for name, command, cwd in specs:
            logger.info("正在启动%s…", name)
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            capture = LoggedProcess(command, cwd, "backend" if name == "后端" else "frontend", echo=True, **options)
            captures.append(capture)
            process = capture.process
            children.append((name, process))
            wait_ready(BACKEND_URL if name == "后端" else FRONTEND_URL, children)
        logger.info("AI audio service 已启动：%s；按 Ctrl+C 同时关闭前后端。", FRONTEND_URL)
        while True:
            for name, process in children:
                if process.poll() is not None:
                    raise RuntimeError(f"{name}已退出（退出码 {process.returncode}），同步关闭另一服务。")
            time.sleep(0.3)
    except KeyboardInterrupt:
        logger.info("正在关闭前后端…")
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        logger.error("启动失败：%s", exc)
        return 1
    finally:
        stop_children(children)
        for capture in captures:
            capture.drain()
        handler.close()
        console.close()
        logger.handlers.clear()


if __name__ == "__main__":
    sys.exit(main())
