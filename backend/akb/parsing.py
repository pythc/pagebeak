"""Bounded local baseline. OCR and semantic retrieval are explicitly not emulated."""

import csv
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from xml.etree.ElementTree import iterparse
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup
from docx import Document
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from pptx import Presentation
from pypdf import PdfReader


class ParseError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass
class Block:
    text: str
    locator: dict


@dataclass
class Parsed:
    blocks: list[Block]
    coverage: str = "complete"
    warning: str | None = None


def table_block(cells, locator, text=None):
    cells = [str(c) for c in cells]
    text = " | ".join(cells) if text is None else text
    # Only unsplit rows have a complete column mapping; never guess for fragments.
    if len(cells) <= 200 and len(text) <= 4000:
        locator = {**locator, "cells": cells}
    return Block(text, locator)


def _input(data):
    return data if isinstance(data, Path) else BytesIO(data)


def _zip_check(data):
    try:
        with ZipFile(_input(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(e.file_size for e in entries) > 100 * 1024 * 1024:
                raise ParseError("unsafe_archive", "文档展开后过大，未进行解析")
    except BadZipFile as exc:
        raise ParseError("invalid_file", "Office 文件损坏或类型不匹配") from exc


def _xlsx_missing_dimension_bounds(data):
    """Some valid exporters omit dimension. Inspect coordinates before row allocation."""
    max_row, max_col = 1, 1
    with ZipFile(_input(data)) as archive:
        for entry in archive.infolist():
            if not (entry.filename.startswith("xl/worksheets/") and entry.filename.endswith(".xml")):
                continue
            with archive.open(entry) as stream:
                row_number, col_number = 0, 0
                for event, element in iterparse(stream, events=("start", "end")):
                    tag = element.tag.rsplit("}", 1)[-1]
                    if event == "end":
                        element.clear()
                        continue
                    if tag == "row":
                        row_number = int(element.get("r", row_number + 1))
                        col_number = 0
                        max_row = max(max_row, row_number)
                    elif tag == "c":
                        address = element.get("r")
                        if address:
                            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", address)
                            if not match:
                                raise ParseError("invalid_file", "工作表单元格坐标无效")
                            max_row = max(max_row, int(match[2]))
                            col_number = column_index_from_string(match[1])
                        else:
                            col_number += 1
                        max_col = max(max_col, col_number)
                    if max_row > 20000 or max_col > 200:
                        raise ParseError("cell_limit", "工作表超过本地解析范围")
    return max_row, max_col


def parse_document(filename: str, data: bytes | Path, *, ocr_page_limit: int = 10) -> Parsed:
    suffix = Path(filename).suffix.lower()
    blocks: list[Block] = []
    coverage, warning = "complete", None
    if suffix in {".docx", ".xlsx", ".pptx"}:
        _zip_check(data)
    try:
        if suffix in {".txt", ".md", ".html", ".htm", ".csv"}:
            if isinstance(data, Path):
                with data.open("rb") as stream:
                    raw = stream.read(8 * 1024**2)
                if data.stat().st_size > len(raw):
                    coverage, warning = "partial", "仅解析前 8 MiB 文本，请拆分大文件以完整索引"
                data = raw
            try:
                text = data.decode("utf-8-sig", errors="ignore" if coverage == "partial" else "strict")
            except UnicodeDecodeError as exc:
                raise ParseError("encoding", "文本文件需使用 UTF-8 编码") from exc
            if suffix in {".html", ".htm"}:
                soup = BeautifulSoup(text, "html.parser")
                for node in soup(["script", "style", "noscript"]):
                    node.decompose()
                text = soup.get_text("\n")
            if suffix == ".csv":
                for index, row in enumerate(csv.reader(text.splitlines()), 1):
                    if any(row):
                        blocks.append(table_block(row, {"kind": "csv_row", "row": index}))
            else:
                for index, line in enumerate(text.splitlines(), 1):
                    if line.strip():
                        blocks.append(Block(line.strip(), {"kind": "line", "line": index}))
        elif suffix == ".pdf":
            if isinstance(data, Path):
                with data.open("rb") as stream:
                    header = stream.read(5)
            else:
                header = data[:5]
            if header != b"%PDF-":
                raise ParseError("invalid_file", "文件不是有效的 PDF")
            reader = PdfReader(_input(data), strict=False)
            if reader.is_encrypted:
                raise ParseError("password_required", "PDF 已加密，暂不支持密码解密")
            if len(reader.pages) > 300:
                raise ParseError("page_limit", "本地版本最多解析 300 页 PDF")
            empty_pages, ocr_pages = [], 0
            for index, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    blocks.append(Block(text.strip(), {"kind": "page", "page": index}))
                else:
                    if ocr_pages < ocr_page_limit:
                        from akb.ocr import ocr_pdf_page

                        recognized = ocr_pdf_page(data, index - 1)
                        blocks.extend(
                            Block(t, {"kind": "page", "page": index, "method": "ocr", **loc})
                            for t, loc in recognized
                        )
                        ocr_pages += 1
                        if not recognized:
                            empty_pages.append(index)
                    else:
                        empty_pages.append(index)
            if ocr_pages:
                coverage, warning = "partial", f"已使用 OCR 识别扫描页，请核对识别结果；每份文件最多 OCR {ocr_page_limit} 页"
            if empty_pages:
                coverage, warning = (
                    "partial",
                    f"第 {', '.join(map(str, empty_pages))} 页未提取到文本；空白页或超出每份文件 {ocr_page_limit} 页 OCR 上限",
                )
        elif suffix == ".docx":
            doc = Document(_input(data))
            for index, paragraph in enumerate(doc.paragraphs, 1):
                if paragraph.text.strip():
                    blocks.append(Block(paragraph.text, {"kind": "paragraph", "paragraph": index}))
            for ti, table in enumerate(doc.tables, 1):
                for ri, row in enumerate(table.rows, 1):
                    blocks.append(
                        table_block([c.text for c in row.cells], {"kind": "table", "table": ti, "row": ri})
                    )
            coverage, warning = "partial", "基础解析覆盖段落和表格；文本框、图片及复杂布局待完整解析器验证"
        elif suffix == ".xlsx":
            # Object storage intentionally has no filename suffix. Pass a stream so openpyxl
            # validates the ZIP content instead of rejecting the extensionless object key.
            with data.open("rb") if isinstance(data, Path) else BytesIO(data) as stream:
                workbook = load_workbook(stream, read_only=True, data_only=False, keep_links=False)
                try:
                    missing_bounds = None
                    for sheet in workbook:
                        if (sheet.max_row or 0) > 20000 or (sheet.max_column or 0) > 200:
                            raise ParseError("cell_limit", "工作表超过本地解析范围")
                        if sheet.max_row is None or sheet.max_column is None:
                            if missing_bounds is None:
                                missing_bounds = _xlsx_missing_dimension_bounds(data)
                            rows = sheet.iter_rows(max_row=missing_bounds[0], max_col=missing_bounds[1])
                        else:
                            rows = sheet.iter_rows()
                        for row_index, row in enumerate(rows, 1):
                            parts = [
                                f"{cell.coordinate}: {str(cell.value)[:4000]}"
                                for cell in row
                                if cell.value is not None
                            ]
                            if parts:
                                blocks.append(
                                    table_block(
                                        ["" if c.value is None else str(c.value) for c in row],
                                        {
                                            "kind": "cells",
                                            "row": row_index,
                                            "sheet": sheet.title,
                                            "range": f"A{row_index}:{get_column_letter(len(row))}{row_index}",
                                        },
                                        " | ".join(parts),
                                    )
                                )
                finally:
                    workbook.close()
            coverage, warning = "partial", "读取单元格及公式文本，不计算公式或解析图表"
        elif suffix == ".pptx":
            presentation = Presentation(_input(data))
            for index, slide in enumerate(presentation.slides, 1):
                text = "\n".join(shape.text for shape in slide.shapes if shape.has_text_frame)
                if text.strip():
                    blocks.append(Block(text, {"kind": "slide", "slide": index}))
            coverage, warning = "partial", "基础解析覆盖幻灯片文本，图片和复杂表格待完整解析器验证"
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            from akb.ocr import ocr_image

            blocks.extend(Block(t, {"kind": "image", "method": "ocr", **loc}) for t, loc in ocr_image(data))
            coverage, warning = "partial", "OCR 识别结果可能有误，请核对；仅处理图片第一帧"
        else:
            raise ParseError("unsupported", "文件已保存，当前版本不支持此格式解析")
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("parse_failed", "文档无法解析，请检查文件是否完整或格式是否正确") from exc
    if not blocks:
        raise ParseError("no_text", "未提取到文本，文件可能需要 OCR")
    bounded = []
    total = 0
    for block in blocks:
        # Retain the original locator when splitting a long page or paragraph.
        for offset in range(0, len(block.text), 4000):
            fragment = block.text[offset : offset + 4000]
            total += len(fragment)
            if len(bounded) >= 5000 or total > 2_000_000:
                return Parsed(bounded, "partial", "已达到本地解析文本上限")
            bounded.append(Block(fragment, {**block.locator, "offset": offset}))
    return Parsed(bounded, coverage, warning)


FIELDS = {
    "name": "姓名",
    "email": "邮箱",
    "organization": "工作单位",
    "job_title": "职称",
    "phone": "联系电话",
    "research_interests": "研究方向",
}
LABELS = {
    "姓名": "name",
    "邮箱": "email",
    "电子邮箱": "email",
    "工作单位": "organization",
    "单位": "organization",
    "职称": "job_title",
    "联系电话": "phone",
    "研究方向": "research_interests",
}


def extract_labeled(text: str):
    for line in text.splitlines():
        match = re.fullmatch(
            r"\s*(姓名|邮箱|电子邮箱|工作单位|单位|职称|联系电话|研究方向)\s*[:：]\s*(.{1,250}?)\s*", line
        )
        if match:
            yield LABELS[match[1]], match[2], line.strip()


def category_for(filename: str, text: str) -> str:
    if re.search(r"论文|paper|doi\s*[:：]|abstract", filename + " " + text[:3000], re.I):
        return "论文与参考文献"
    if re.search(r"简历|履历|姓名\s*[:：]|curriculum", filename + " " + text[:2000], re.I):
        return "个人与工作"
    if re.search(r"项目|project", filename + " " + text[:1000], re.I):
        return "项目资料"
    return "其他资料"
