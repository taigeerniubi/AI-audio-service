"""将表格单独序列化为 XML 并按保留表头的行组切片。"""

import hashlib
import io
import xml.etree.ElementTree as ET

from backend.llm.qwen.embedding import token_count, validate_text


def table_chunks(rows, name, table_id, settings, pages=None, caption="", header_rows=1, origin="", bbox=None):
    """将完整表格转为 XML，并按行组切分且重复表头与跨行上下文。

    Args:
        rows: 包含原行号及单元格文本、跨度的表格行。
        name: 原始资料文件名。
        table_id: 该资料内的表格标识。
        settings: 分词器和单片段预算配置。
        pages: 表格来源页码。
        caption: 附近标题或表格说明。
        header_rows: 开头作为表头候选的行数。
        origin: 解析来源，例如 pdf_grid。
        bbox: 可选表格坐标。

    Returns:
        检索片段列表和完整表格 XML。

    Raises:
        ValueError: 表头加一行已超过预算，无法在保留单元格的条件下拆分。
    """
    if not rows:
        return [], ""
    headers, data_rows = rows[:header_rows], rows[header_rows:]

    def serialize(selected):
        """把选定行与共享表头、跨片段合并单元格序列化。

        Args:
            selected: 本片段的数据行，不含已分离的表头候选。

        Returns:
            由 XML 库转义后的表格字符串。
        """
        root = ET.Element(
            "table",
            {"source": name, "id": table_id, "origin": origin, "pages": ",".join(map(str, pages or []))},
        )
        if caption:
            ET.SubElement(root, "caption").text = caption
        if bbox:
            root.set("bbox", ",".join(str(round(v, 2)) for v in bbox))
        for group_name, group in [("header_candidates", headers), ("rows", selected)]:
            group_node = ET.SubElement(root, group_name)
            for row in group:
                node = ET.SubElement(group_node, "row", {"index": str(row["index"])})
                for cell in row["cells"]:
                    element = ET.SubElement(node, "cell", {k: str(v) for k, v in cell.items() if k != "text"})
                    element.text = cell.get("text", "")
        if selected:
            continued = []
            first = selected[0]["index"]
            for row in data_rows:
                for cell in row["cells"]:
                    if row["index"] < first < row["index"] + cell.get("rowspan", 1):
                        continued.append({**cell, "anchor_row": row["index"]})
            if continued:
                node = ET.SubElement(root, "continued_cells")
                for cell in continued:
                    ET.SubElement(
                        node, "cell", {k: str(v) for k, v in cell.items() if k != "text"}
                    ).text = cell["text"]
        return ET.tostring(root, encoding="unicode", short_empty_elements=False)

    chunks, pending = [], []

    def append(group):
        """校验行组预算并追加含来源元信息的表格片段。

        Args:
            group: 已经按完整行组合好的数据行。
        """
        text = serialize(group)
        validate_text(text, settings, settings.chunk_tokens)
        chunks.append(
            {
                "text": text,
                "source": name,
                "kind": "table_xml",
                "table_id": table_id,
                "pages": pages or [],
                "headings": [caption] if caption else [],
                "source_urls": [],
                "row_start": group[0]["index"] if group else None,
                "row_end": group[-1]["index"] if group else None,
                "tokens": token_count(text, settings),
                "chunk_id": hashlib.sha256(text.encode()).hexdigest()[:24],
            }
        )

    for row in data_rows:
        candidate = serialize([*pending, row])
        if token_count(candidate, settings) > settings.chunk_tokens or len(candidate.encode()) > 8000:
            if not pending:
                raise ValueError("表头与单行表格已超过切片预算，请拆分宽表/长单元格；未截断单元格")
            append(pending)
            pending = []
        pending.append(row)
    if pending or not data_rows:
        append(pending)
    return chunks, serialize(data_rows)


def pdf_document(data, name, settings):
    """提取 PDF 格线表格为 XML，并把表格外文字放入 DoclingDocument。

    Args:
        data: 带有效文字层的 PDF 字节。
        name: 资料文件名。
        settings: 表格切片的 tokenizer 和预算配置。

    Returns:
        仅含正文的 DoclingDocument、表格片段列表和完整 XML 列表。
    """
    import pdfplumber
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem

    document = DoclingDocument(name=name)
    chunks, tables = [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            document.add_heading(text=f"{name} / 第 {number} 页", level=1)
            found = [t for t in page.find_tables() if len(t.rows) >= 2 and len(t.columns) >= 2]
            for index, table in enumerate(found, 1):
                matrix = table.extract()
                rows = [
                    {
                        "index": i + 1,
                        "cells": [{"col": c + 1, "text": value or ""} for c, value in enumerate(row)],
                    }
                    for i, row in enumerate(matrix)
                ]
                bbox = table.bbox
                # Preserve nearby title/units without treating them as instructions.
                caption = (
                    (page.crop((0, max(0, bbox[1] - 60), page.width, bbox[1])).extract_text() or "")[-240:]
                    if bbox[1] > 0
                    else ""
                )
                result, full_xml = table_chunks(
                    rows,
                    name,
                    f"p{number}-t{index}",
                    settings,
                    pages=[number],
                    caption=caption,
                    origin="pdf_grid",
                    bbox=bbox,
                )
                chunks.extend(result)
                tables.append(full_xml)

            def outside(obj):
                """排除落在已识别表格中的字符，防止正文重复收录表格。

                Args:
                    obj: pdfplumber 页面对象。

                Returns:
                    对象应保留在正文提取区域时返回 True。
                """
                if obj.get("object_type") != "char":
                    return True
                x, y = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
                return not any(t.bbox[0] <= x <= t.bbox[2] and t.bbox[1] <= y <= t.bbox[3] for t in found)

            text = (page.filter(outside).extract_text() or "").strip()
            if text:
                document.add_text(
                    label=DocItemLabel.TEXT,
                    text=text,
                    prov=ProvenanceItem(
                        page_no=number,
                        charspan=(0, len(text)),
                        bbox=BoundingBox(
                            l=0, t=0, r=page.width, b=page.height, coord_origin=CoordOrigin.TOPLEFT
                        ),
                    ),
                )
    return document, chunks, tables


def separate_docling_tables(document, name, settings):
    """保留表格标题、页码及跨度，转换后从正文树中移除表格。

    Args:
        document: 待原位分离表格的 DoclingDocument。
        name: 原始资料文件名。
        settings: 表格切片配置。

    Returns:
        带来源的表格切片与完整 XML 列表。
    """
    chunks, full_tables = [], []
    contexts = {}
    heading = ""
    for item, _ in document.iterate_items():
        if str(item.label.value) in {"title", "section_header"}:
            heading = item.text
        if str(item.label.value) == "table":
            contexts[item.self_ref] = heading
    for number, table in enumerate(document.tables, 1):
        grouped = {}
        header_rows = set()
        for cell in table.data.table_cells:
            index = cell.start_row_offset_idx + 1
            grouped.setdefault(index, []).append(
                {
                    "col": cell.start_col_offset_idx + 1,
                    "rowspan": cell.row_span,
                    "colspan": cell.col_span,
                    "text": cell.text,
                }
            )
            if cell.column_header:
                header_rows.add(index)
        rows = [
            {"index": i, "cells": sorted(cells, key=lambda c: c["col"])}
            for i, cells in sorted(grouped.items())
        ]
        pages = sorted({p.page_no for p in table.prov})
        caption = table.caption_text(document) if table.captions else contexts.get(table.self_ref, "")
        if table.parent:
            parent = table.parent.resolve(document)
            if getattr(parent, "name", "") not in {"", "_root_", "body"}:
                caption = f"{parent.name} / {caption}".strip(" /")
        result, xml = table_chunks(
            rows,
            name,
            f"t{number}",
            settings,
            pages=pages,
            caption=caption,
            header_rows=max(header_rows, default=1),
            origin="office",
        )
        chunks.extend(result)
        full_tables.append(xml)
    document.delete_items(node_items=list(document.tables))
    return chunks, full_tables
