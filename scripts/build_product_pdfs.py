"""将已核对的官网产品事实排版为带来源的中文 PDF 样本。"""

import json
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]


def main():
    """校验采集证据后，使用本机中文字体生成公开产品 PDF。"""
    products = json.loads((ROOT / "samples/products.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "data/crawl/manifest.json").read_text(encoding="utf-8"))
    crawled = {r["url"]: r for r in manifest}
    pdfmetrics.registerFont(TTFont("Song", "C:/Windows/Fonts/simsun.ttc", subfontIndex=0))
    font = "Song"
    styles = {
        "title": ParagraphStyle(
            "title",
            fontName=font,
            fontSize=29,
            leading=36,
            textColor=colors.HexColor("#28213d"),
            spaceAfter=9,
        ),
        "body": ParagraphStyle(
            "body", fontName=font, fontSize=10.5, leading=17, spaceAfter=7, wordWrap="CJK"
        ),
        "section": ParagraphStyle(
            "section",
            fontName=font,
            fontSize=12,
            leading=17,
            textColor=colors.HexColor("#6a4bb2"),
            spaceBefore=12,
            spaceAfter=6,
        ),
        "small": ParagraphStyle(
            "small",
            fontName=font,
            fontSize=8,
            leading=12,
            textColor=colors.HexColor("#65616d"),
            wordWrap="CJK",
            alignment=TA_LEFT,
        ),
    }
    folder = ROOT / "output/pdf"
    folder.mkdir(parents=True, exist_ok=True)
    for product in products:
        if any(url not in crawled for url in product["sources"]):
            raise ValueError("缺少官网抓取证据，请先运行 scripts.crawl_products")
        story = []

        def add(text, kind="body"):
            """转义产品文本并追加指定样式的排版段落。

            Args:
                text: 待显示的产品事实或来源说明。
                kind: 已定义的段落样式名称。
            """
            story.append(Paragraph(escape(text), styles[kind]))

        add("PRODUCT FACT SHEET  /  公开产品资料", "small")
        story.append(Spacer(1, 16))
        add(product["name"], "title")
        add(product["category"])
        add(f"核对日期：{product['verified_on']}  |  币种：USD 美元  |  本地销售顾问演示样本", "small")
        add("01  产品与客户", "section")
        add(product["audience"])
        add("02  套餐与价格", "section")
        if product["slug"] == "tally":
            values = [
                ["Tally 套餐", "月付（美元/月）", "年付（美元/年）"],
                ["Free", "0", "0"],
                ["Pro", "29", "290"],
                ["Business", "89", "890"],
            ]
            table = Table(
                [[Paragraph(escape(v), styles["body"]) for v in row] for row in values],
                colWidths=[167, 170, 170],
            )
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#cfc7df")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2eef9")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.append(table)
            story.append(Spacer(1, 10))
        for plan in product["plans"]:
            details = (
                plan["features"] if product["slug"] == "tally" else f"{plan['price']}。{plan['features']}"
            )
            add(f"{product['name']} {plan['name']}：{details}")
        add("03  报价边界", "section")
        add(product["limits"])
        add("04  需求了解建议", "section")
        add(product["discovery"])
        add("来源与时效", "section")
        add("以上为公开页面的归纳，不代表经销授权或正式报价；采购前以官网与合同为准。", "small")
        for url in product["sources"]:
            add(url, "small")
            add(f"采集时间：{crawled[url]['fetched_at']}", "small")
        path = folder / f"{product['name'].replace('.', '_')}_产品资料.pdf"
        doc = SimpleDocTemplate(
            str(path),
            pagesize=(595.28, 841.89),
            rightMargin=44,
            leftMargin=44,
            topMargin=38,
            bottomMargin=38,
            title=f"{product['name']} 产品资料",
            author="声场产品研究",
        )

        def footer(canvas, document):
            """绘制每页的来源说明、分隔线和页码。

            Args:
                canvas: ReportLab 当前页面画布。
                document: 包含当前页码的文档对象。
            """
            canvas.setStrokeColor(colors.HexColor("#e0dbe9"))
            canvas.line(44, 33, 551, 33)
            canvas.setFont(font, 8)
            canvas.setFillColor(colors.HexColor("#777080"))
            canvas.drawString(44, 20, "公开信息快照 · 产品事实与演示建议已分别标注")
            canvas.drawRightString(551, 20, str(document.page))

        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        print(path)


if __name__ == "__main__":
    main()
