"""离线测量应用初始化与各格式文档解析的进程峰值内存，不调用云端模型。"""

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def peak_memory() -> dict:
    """读取当前进程的操作系统峰值内存计数。

    Returns:
        内存数值与测量方法；不包含其他进程、容器缓存或操作系统占用。
    """
    if sys.platform == "win32":
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                *[
                    (name, ctypes.c_size_t)
                    for name in (
                        "PeakWorkingSetSize",
                        "WorkingSetSize",
                        "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage",
                        "PagefileUsage",
                        "PeakPagefileUsage",
                        "PrivateUsage",
                    )
                ],
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "peak_rss_mib": round(counters.PeakWorkingSetSize / 1024**2, 2),
            "private_commit_mib_at_end": round(counters.PrivateUsage / 1024**2, 2),
            "method": "Windows GetProcessMemoryInfo / PeakWorkingSetSize",
        }
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024**2 if sys.platform == "darwin" else 1024
    return {"peak_rss_mib": round(value / divisor, 2), "method": "getrusage(RUSAGE_SELF).ru_maxrss"}


def worker(kind: str, source: Path, output: Path, tokenizer: Path) -> None:
    """在独立进程中初始化应用或调用真实解析函数，并写入测量结果。

    Args:
        kind: app 或 parse。
        source: 文档路径；app 模式下未使用。
        output: 本次测量 JSON 输出路径。
        tokenizer: 本地 Qwen tokenizer 文件。
    """
    started = time.perf_counter()
    result = {"kind": kind}
    try:
        if kind == "app":
            # .env 不覆盖已设置的变量；隔离真实数据库并明确清空云端凭证。
            os.environ.update(
                DASHSCOPE_API_KEY="", DASHSCOPE_WORKSPACE_ID="", DATABASE_PATH=str(output.with_suffix(".db"))
            )
            from backend.main import app

            result["routes"] = len(app.routes)
            app.state.store.close()
        else:
            from backend.core.config import Settings
            from backend.services.knowledge.ingestion import parse_document

            data = source.read_bytes()
            result.update(source=source.name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            parsed = parse_document(source.name, data, Settings(tokenizer_path=str(tokenizer)))
            result.update(chunks=len(parsed["chunks"]), tables=parsed["table_count"], parser=parsed["parser"])
        result["status"] = "ok"
    except Exception as exc:
        # 不记录环境变量或异常正文，避免将凭证带入可提交报告。
        result.update(status="error", error_type=type(exc).__name__)
    result.update(elapsed_seconds=round(time.perf_counter() - started, 3), **peak_memory())
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def create_fixtures(folder: Path) -> list[Path]:
    """生成固定规模的合成资料，用于比较解析资源消耗。

    Args:
        folder: 临时资料目录。

    Returns:
        Markdown、DOCX、XLSX 和 50 页文字 PDF 的路径。
    """
    from docx import Document
    from openpyxl import Workbook
    from reportlab.pdfgen.canvas import Canvas

    text = "产品提供客户管理、销售邀约、知识检索与会话报告；价格仅为测试数据，不能作为报价。"
    markdown = folder / "synthetic_40_sections.md"
    markdown.write_text("\n\n".join(f"## 产品 {i}\n{text * 4}" for i in range(40)), encoding="utf-8")
    word = folder / "synthetic_40_sections.docx"
    doc = Document()
    for index in range(40):
        doc.add_heading(f"产品 {index}", level=1)
        doc.add_paragraph(text * 4)
    table = doc.add_table(rows=1, cols=3)
    for cell, value in zip(table.rows[0].cells, ["产品", "测试价格", "限制"], strict=True):
        cell.text = value
    for index in range(30):
        for cell, value in zip(
            table.add_row().cells, [f"产品{index}", str(index + 99), "仅测试"], strict=True
        ):
            cell.text = value
    doc.save(word)
    excel = folder / "synthetic_250_rows.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["产品", "测试月价", "客户类型", "限制"])
    for index in range(250):
        sheet.append([f"产品{index}", index + 99, "中小团队", "仅测试，非实际报价"])
    book.save(excel)
    book.close()
    pdf = folder / "synthetic_50_pages.pdf"
    canvas = Canvas(str(pdf))
    for page in range(50):
        canvas.drawString(40, 800, f"Resource benchmark page {page + 1}. Synthetic product facts only.")
        for row in range(25):
            canvas.drawString(
                40, 770 - 25 * row, f"Feature {row}: knowledge search, reports and appointments."
            )
        canvas.showPage()
    canvas.save()
    return [markdown, word, excel, pdf]


def main() -> None:
    """依次运行隔离子进程，保存可复查但不代表云服务器压测的报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output" / f"resource-benchmark-{datetime.now().strftime('%Y%m%d')}" / "results.json",
    )
    parser.add_argument("--tokenizer", type=Path, default=ROOT / ".cache/tokenizers/qwen3/tokenizer.json")
    parser.add_argument("--worker", choices=["app", "parse"], help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.source, args.output, args.tokenizer)
        return
    if not args.tokenizer.is_file():
        parser.error("缺少本地 tokenizer；请先运行 scripts.setup_rag")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cache = ROOT / ".cache/resource-benchmark"
    cache.mkdir(parents=True, exist_ok=True)
    measurements = []
    with tempfile.TemporaryDirectory(dir=cache) as temp:
        folder = Path(temp)
        cases = [("app", ROOT)]
        cases.extend(("parse", path) for path in sorted((ROOT / "output/pdf").glob("*.pdf")))
        cases.extend(("parse", path) for path in create_fixtures(folder))
        for index, (kind, source) in enumerate(cases):
            result_file = folder / f"result-{index}.json"
            command = [
                sys.executable,
                "-m",
                "scripts.benchmark_resources",
                "--worker",
                kind,
                "--source",
                str(source),
                "--output",
                str(result_file),
                "--tokenizer",
                str(args.tokenizer.resolve()),
            ]
            try:
                subprocess.run(command, cwd=ROOT, capture_output=True, timeout=90, check=True)
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except (subprocess.SubprocessError, OSError) as exc:
                result = {
                    "kind": kind,
                    "source": source.name,
                    "status": "error",
                    "error_type": type(exc).__name__,
                }
            measurements.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "native_thread_environment": {
            name: os.environ.get(name)
            for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
        },
        "versions": {
            name: importlib.metadata.version(name)
            for name in (
                "docling-slim",
                "docling-core",
                "transformers",
                "pdfplumber",
                "pypdf",
            )
        },
        "scope": "Fresh process per case; no OCR, embeddings, model calls, HTTP load or container limit.",
        "measurements": measurements,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(item["status"] != "ok" for item in measurements):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
