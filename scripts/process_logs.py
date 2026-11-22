"""以 UTF-8 收集子进程输出到项目 logs 目录，同时保留可选的终端输出。"""

import argparse
import logging
import os
import re
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def file_handler(path: Path) -> RotatingFileHandler:
    """创建追加写入的 UTF-8 日志处理器，并限制单个日志文件大小。

    Args:
        path: 日志文件路径。

    Returns:
        每个文件最多 5 MiB、保留三份历史文件的处理器。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3,
                                  encoding="utf-8", errors="backslashreplace")
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


class LoggedProcess:
    """收集一个子进程的标准输出与错误输出，进程生命周期仍由调用方管理。"""

    def __init__(self, command, cwd, name, log_dir=LOG_DIR, echo=False, **options):
        """启动子进程并异步收集两个输出流。

        Args:
            command: 不经过 shell 解释的命令参数列表。
            cwd: 子进程工作目录。
            name: 日志文件前缀。
            log_dir: 日志目录。
            echo: 是否同时输出到当前终端。
            **options: 进程组等 Popen 选项。

        Raises:
            ValueError: 日志名称含有非法字符。
            OSError: 无法创建日志或启动进程。
        """
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", name):
            raise ValueError("Invalid process log name")
        self.handlers = []
        self.threads = []
        try:
            for stream in ("stdout", "stderr"):
                self.handlers.append(file_handler(Path(log_dir) / f"{name}.{stream}.log"))
            env = {**os.environ, **options.pop("env", {}), "PYTHONUTF8": "1",
                   "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"}
            self.process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                            errors="backslashreplace", bufsize=1, **options)
        except BaseException:
            for handler in self.handlers:
                handler.close()
            raise
        for stream, handler, target in zip((self.process.stdout, self.process.stderr), self.handlers,
                                            (sys.stdout, sys.stderr), strict=True):
            thread = threading.Thread(target=self._relay, args=(stream, handler, target if echo else None), daemon=True)
            thread.start()
            self.threads.append(thread)

    @staticmethod
    def _relay(stream, handler, target):
        """逐行写日志，管道跨块的多字节字符由文本解码器保留。

        Args:
            stream: 已设置 UTF-8 的子进程输出流。
            handler: 对应文件处理器。
            target: 可选的终端输出流。
        """
        try:
            for line in stream:
                text = ANSI.sub("", line.rstrip("\r\n"))
                handler.handle(logging.LogRecord("process", logging.INFO, "", 0, text, (), None))
                if target is not None:
                    try:
                        target.write(line)
                        target.flush()
                    except (OSError, UnicodeError):
                        # 终端编码或关闭不能阻止日志继续保存。
                        target = None
        finally:
            stream.close()
            handler.close()

    def drain(self):
        """进程停止后等待尾部输出写入文件，避免正常退出时丢失日志。"""
        for thread in self.threads:
            thread.join(timeout=5)


def main() -> int:
    """在 Windows 作业对象内部包装后台进程，转交其真实退出码。

    Returns:
        子进程退出码。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A child command is required")
    capture = LoggedProcess(command, Path(__file__).resolve().parents[1], args.name)
    try:
        return capture.process.wait()
    finally:
        if capture.process.poll() is None:
            capture.process.terminate()
            try:
                capture.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                capture.process.kill()
                capture.process.wait(timeout=5)
        capture.drain()


if __name__ == "__main__":
    raise SystemExit(main())
