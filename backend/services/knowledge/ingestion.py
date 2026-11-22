"""在隔离进程解析文档并原子构建知识库索引。"""

import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from backend.core.paths import PROJECT_ROOT
from backend.llm.factory import LLMFactory
from backend.llm.qwen.embedding import token_count, tokenizer_digest, validate_text

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CHUNKS = 200
SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".xlsx"}


def preflight(name, data):
    """在解析或启动子进程前检查格式、文件大小及压缩包边界。

    Args:
        name: 用于识别格式的资料文件名。
        data: 完整文件字节内容。

    Raises:
        ValueError: 文件为空、格式不支持、加密、损坏或超过限额。
    """
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError("支持 TXT、MD、PDF、DOCX、XLSX；旧版 DOC/XLS 请另存为新格式")
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("文件为空或超过 10 MB")
    if suffix in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                info = archive.infolist()
                if len(info) > 3000 or sum(i.file_size for i in info) > 30 * 1024 * 1024:
                    raise ValueError("Office 文件解压后过大，请拆分文档")
                if any(i.flag_bits & 1 for i in info):
                    raise ValueError("不支持加密 Office 文件")
                required = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
                if required not in archive.namelist():
                    raise ValueError("文件内容与扩展名不一致")
        except zipfile.BadZipFile as exc:
            raise ValueError("Office 文件已损坏") from exc
    if suffix == ".pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("不是有效的 PDF 文件")
    if suffix in {".txt", ".md"}:
        try:
            text = data.decode("utf-8-sig").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("TXT/MD 须使用 UTF-8 编码") from exc
        if not text or "\x00" in text:
            raise ValueError("资料为空或包含二进制内容")


def parse_document(name, data, settings):
    """解析正文与独立表格，按本地 Token 预算生成带来源的切片。

    Args:
        name: 用于识别格式的资料文件名。
        data: 完整文件字节内容。
        settings: 本地 tokenizer 路径和切片预算配置。

    Returns:
        正文、切片、XML 表格、来源摘要及解析警告；不包含向量。

    Raises:
        ValueError: 页数、单元格、正文或切片数量超限，或文档不适合当前解析器。
    """
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.document_converter import DocumentConverter
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import PreTrainedTokenizerFast

    from backend.services.knowledge.tables import pdf_document, separate_docling_tables

    preflight(name, data)
    suffix = Path(name).suffix.lower()
    warnings = []
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("不支持加密 PDF，请上传可读取的副本")
        if len(reader.pages) > 50:
            raise ValueError("单份 PDF 最多 50 页，请拆分后上传")
        if any(len((p.extract_text() or "").strip()) < 15 for p in reader.pages):
            raise ValueError("PDF 含无有效文字层的页面；当前未启用 OCR，请先识别扫描页")
        warnings.append(
            "PDF 格线表格单独转 XML，首行是表头候选；未启用 OCR，无边框/跨页/合并复杂表格需核对，建议上传 XLSX/DOCX"
        )
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        try:
            if sum(s.max_row * s.max_column for s in book) > 50000:
                raise ValueError("表格超过 50,000 个单元格，请拆分后上传")
            if any(c.data_type == "f" for s in book for row in s for c in row):
                raise ValueError("XLSX 含公式；为避免读取陈旧或缺失缓存，请复制并粘贴为值后上传")
        finally:
            book.close()
    source_name = name
    if suffix == ".txt":
        name = str(Path(name).with_suffix(".md"))
    if suffix == ".pdf":
        document, table_parts, tables = pdf_document(data, source_name, settings)
    else:
        converter = DocumentConverter(allowed_formats=[InputFormat.MD, InputFormat.DOCX, InputFormat.XLSX])
        result = converter.convert(DocumentStream(name=name, stream=io.BytesIO(data)), max_num_pages=50)
        if str(result.status.value) != "success":
            raise ValueError("文档未完整解析，未保存部分索引，请检查文件")
        document = result.document
        table_parts, tables = separate_docling_tables(document, source_name, settings)
    text = document.export_to_markdown() + "\n\n" + "\n\n".join(tables)
    if not text.strip() or len(text) > 500000:
        raise ValueError("未提取到文字或文档超过 500,000 字符")
    hf = PreTrainedTokenizerFast(tokenizer_file=settings.tokenizer_path)
    tokenizer = HuggingFaceTokenizer(tokenizer=hf, max_tokens=settings.chunk_tokens)
    chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True, repeat_table_header=True)
    chunks = list(table_parts)
    for chunk in chunker.chunk(dl_doc=document):
        content = chunker.contextualize(chunk=chunk).strip()
        if not content:
            continue
        # Count AFTER contextualization (headings/table labels count too).
        validate_text(content, settings, settings.chunk_tokens)
        pages = sorted({p.page_no for item in chunk.meta.doc_items for p in item.prov})
        chunks.append(
            {
                "text": content,
                "source": source_name,
                "headings": chunk.meta.headings or [],
                "pages": pages,
                "tokens": token_count(content, settings),
                "source_urls": sorted(set(re.findall(r"https?://[^\s<>\])]+", content))),
                "chunk_id": hashlib.sha256(content.encode()).hexdigest()[:24],
            }
        )
        if len(chunks) > MAX_CHUNKS:
            raise ValueError("切片超过 200 个，请拆分文档；尚未调用 Embedding")
    if not chunks:
        raise ValueError("没有可检索的文本切片")
    if len(chunks) > MAX_CHUNKS:
        raise ValueError("切片超过 200 个，请拆分文档；尚未调用 Embedding")
    document_urls = sorted(set(re.findall(r"https?://[^\s<>\])]+", text)))
    for chunk in chunks:
        if not chunk["source_urls"]:
            chunk["source_urls"] = document_urls
    return {
        "text": text,
        "chunks": chunks,
        "warnings": warnings,
        "parser": "pdfplumber+docling-document" if suffix == ".pdf" else "docling",
        "table_count": len(tables),
        "tables_xml": tables,
        "chunker": "HybridChunker",
        "chunk_tokens": settings.chunk_tokens,
        "tokenizer_sha256": tokenizer_digest(settings),
        "file_sha256": hashlib.sha256(data).hexdigest(),
    }


def parse_isolated(name, data, settings):
    """在一次性子进程中解析资料，超时终止并清理临时文件。

    Args:
        name: 用于识别格式的资料文件名。
        data: 完整文件字节内容。
        settings: 传给隔离进程的 tokenizer 和切片配置。

    Returns:
        解析器输出的完整文档结构。

    Raises:
        ValueError: 预检查、解析失败或运行超过 90 秒。
    """
    preflight(name, data)
    root = PROJECT_ROOT
    temp_root = root / ".cache" / "ingestion"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_root) as folder:
        source = Path(folder) / ("input" + Path(name).suffix.lower())
        source.write_bytes(data)
        request = Path(folder) / "request.json"
        output = Path(folder) / "result.json"
        request.write_text(
            json.dumps(
                {
                    "name": name,
                    "source": str(source),
                    "output": str(output),
                    "tokenizer_path": str(Path(settings.tokenizer_path).resolve()),
                    "chunk_tokens": settings.chunk_tokens,
                }
            ),
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "backend.services.knowledge.ingestion", str(request)],
                cwd=root,
                capture_output=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("文档解析超过 90 秒，已终止解析进程；请拆分后重试") from exc
        if not output.exists():
            raise ValueError("Docling 解析进程失败，请检查依赖安装或文件格式")
        result = json.loads(output.read_text(encoding="utf-8"))
        if completed.returncode or "error" in result:
            raise ValueError(result.get("error", "Docling 无法解析此文件"))
        return result


def build_index(parsed, settings, embedder=None):
    """为全部切片请求向量并组装索引，成功前不写入资料表。

    Args:
        parsed: 已完成解析的正文、切片和元信息。
        settings: 向量模型和 Token 预算配置。
        embedder: 可选向量客户端；留空时由模型工厂创建。

    Returns:
        带向量、模型版本、地域、维度及实际用量的完整索引。

    Raises:
        ValueError: 切片不满足预算或向量数量不匹配。
    """
    for chunk in parsed["chunks"]:
        validate_text(chunk["text"], settings, settings.chunk_tokens)
    vectors, usages = (embedder or LLMFactory(settings).create_embedding()).embed([c["text"] for c in parsed["chunks"]])
    if len(vectors) != len(parsed["chunks"]):
        raise ValueError("向量数量与切片数量不匹配")
    return {
        **parsed,
        "model": settings.embedding_model,
        "dimension": settings.embedding_dimension,
        "region": settings.region,
        "vectors": vectors,
        "usage": usages,
    }


if __name__ == "__main__":
    from backend.core.config import Settings

    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        payload = parse_document(
            request["name"],
            Path(request["source"]).read_bytes(),
            Settings(tokenizer_path=request["tokenizer_path"], chunk_tokens=request["chunk_tokens"]),
        )
    except ValueError as exc:
        payload = {"error": str(exc)[:500]}
    except Exception as exc:
        payload = {"error": f"Docling 解析失败（{type(exc).__name__}），请检查格式或依赖"}
    Path(request["output"]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    sys.exit(1 if "error" in payload else 0)
