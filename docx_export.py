"""把研究报告的 Markdown 导出成 Word（.docx）。

为什么不直接用 pandoc/LibreOffice：项目坚持“无额外系统依赖、无构建”；
``python-docx`` 是纯 Python 包（lxml 轮子），装完就能跑，失败也只是导出接口 503，
不影响研究主流程。

支持的报告语法（对应 reporting.py 实际产出的子集）：
标题 / 段落 / 无序列表 / 有序列表 / 引用 / ``**粗体**`` / 行内代码 /
``[S#]`` 上标引用 / ``[文字](url)`` 超链接 / Markdown 表格。
"""

from __future__ import annotations

import io
import re
from typing import Any

_INLINE = re.compile(
    r"(\*\*.+?\*\*"          # **粗体**
    r"|`[^`]+`"              # `行内代码`
    r"|\[S\d+\](?:\([^)]*\))?"  # [S1] 或 [S1](url)
    r"|\[[^\]]+\]\([^)]+\)"  # [文字](url)
    r")"
)
_CITE = re.compile(r"\[(S\d+)\](?:\(([^)]*)\))?")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_TABLE_SEP = re.compile(r"^:?-{2,}:?$")


class DocxUnavailable(RuntimeError):
    """没装 python-docx：调用方应转成 503 并提示安装命令。"""


def _require_docx() -> Any:
    try:
        import docx  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - 依赖缺失分支
        raise DocxUnavailable("导出 Word 需要 python-docx：pip install python-docx") from exc
    return docx


def _set_base_font(doc: Any) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    for name in ("Heading 1", "Heading 2", "Heading 3", "Title"):
        try:
            heading = doc.styles[name]
        except KeyError:
            continue
        heading.font.name = "Microsoft YaHei"
        hrpr = heading.element.get_or_add_rPr()
        hfonts = hrpr.find(qn("w:rFonts"))
        if hfonts is None:
            hfonts = hrpr.makeelement(qn("w:rFonts"), {})
            hrpr.append(hfonts)
        hfonts.set(qn("w:eastAsia"), "Microsoft YaHei")


def _add_hyperlink(paragraph: Any, text: str, url: str) -> None:
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    try:
        rel_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    except (ValueError, KeyError):
        paragraph.add_run(text)
        return
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1F6FEB")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(color)
    props.append(underline)
    run.append(props)
    node = OxmlElement("w:t")
    node.text = text
    node.set(qn("xml:space"), "preserve")
    run.append(node)
    link.append(run)
    paragraph._p.append(link)


def _add_inline(paragraph: Any, text: str) -> None:
    position = 0
    for match in _INLINE.finditer(text):
        if match.start() > position:
            paragraph.add_run(text[position:match.start()])
        token = match.group(0)
        if token.startswith("**") and token.endswith("**"):
            paragraph.add_run(token[2:-2]).bold = True
        elif token.startswith("`"):
            paragraph.add_run(token[1:-1]).font.name = "Consolas"
        elif token.startswith("[S"):
            found = _CITE.match(token)
            label = found.group(1) if found else token.strip("[]")
            paragraph.add_run(label).font.superscript = True
        else:
            found = _LINK.match(token)
            if found:
                _add_hyperlink(paragraph, found.group(1), found.group(2))
            else:
                paragraph.add_run(token)
        position = match.end()
    if position < len(text):
        paragraph.add_run(text[position:])


def _plain(text: str) -> str:
    """标题类文本去掉行内标记（加粗/代码/链接语法）。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def _style_paragraph(doc: Any, style_name: str) -> Any:
    try:
        return doc.add_paragraph(style=style_name)
    except KeyError:  # 模板里缺样式时退回普通段落
        return doc.add_paragraph()


def _add_table(doc: Any, raw_rows: list[str]) -> None:
    rows: list[list[str]] = []
    for raw in raw_rows:
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if cells and all(_TABLE_SEP.match(cell) for cell in cells if cell):
            continue  # 表头分隔行
        rows.append(cells)
    if not rows:
        return
    width = max(len(row) for row in rows)
    table = doc.add_table(rows=len(rows), cols=width)
    table.style = "Table Grid"
    for row_index, row in enumerate(rows):
        for col_index in range(width):
            cell = table.cell(row_index, col_index)
            cell.text = ""
            _add_inline(cell.paragraphs[0], row[col_index] if col_index < len(row) else "")


def render_markdown(doc: Any, markdown: str) -> None:
    lines = (markdown or "").splitlines()
    index = 0
    heading_levels = {1: 1, 2: 2, 3: 3, 4: 3}
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        # 表格：连续以 | 开头的行，且第二行是分隔行
        if stripped.startswith("|") and index + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[index + 1]):
            block: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            _add_table(doc, block)
            continue
        match = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if match:
            level = heading_levels[len(match.group(1))]
            doc.add_heading(_plain(match.group(2)), level=level)
            index += 1
            continue
        if re.match(r"^[-*]\s+", stripped):
            paragraph = _style_paragraph(doc, "List Bullet")
            _add_inline(paragraph, re.sub(r"^[-*]\s+", "", stripped))
            index += 1
            continue
        if re.match(r"^\d+\.\s+", stripped):
            paragraph = _style_paragraph(doc, "List Number")
            _add_inline(paragraph, re.sub(r"^\d+\.\s+", "", stripped))
            index += 1
            continue
        if stripped.startswith(">"):
            paragraph = _style_paragraph(doc, "Quote")
            _add_inline(paragraph, stripped.lstrip("> ").strip())
            index += 1
            continue
        paragraph = doc.add_paragraph()
        _add_inline(paragraph, stripped)
        index += 1


def markdown_to_docx(
    markdown: str,
    *,
    title: str = "",
    meta: list[tuple[str, str]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> bytes:
    """把报告 Markdown 渲染成 docx 字节；失败抛 DocxUnavailable（依赖缺失）。"""
    docx = _require_docx()
    from docx.shared import Pt, RGBColor

    doc = docx.Document()
    _set_base_font(doc)

    if title:
        doc.add_heading(_plain(title), level=0)
    if meta:
        paragraph = doc.add_paragraph()
        for label, value in meta:
            run = paragraph.add_run(f"{label}：{value}    ")
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    render_markdown(doc, markdown)

    if evidence:
        doc.add_heading("附录：证据表", level=2)
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        headers = ("编号", "结论 / 摘要", "来源", "可信度")
        for index, text in enumerate(headers):
            cell = table.cell(0, index)
            cell.text = ""
            cell.paragraphs[0].add_run(text).bold = True
        for item in evidence[:40]:
            row = table.add_row().cells
            claim = str(item.get("claim") or item.get("evidence_text") or "")[:200]
            url = str(item.get("source_url") or "")
            values = (
                str(item.get("id") or item.get("source_id") or "-"),
                claim,
                url,
                str(item.get("confidence") or "-"),
            )
            for index, text in enumerate(values):
                row[index].text = ""
                _add_inline(row[index].paragraphs[0], text)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
