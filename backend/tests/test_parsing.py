import re
from io import BytesIO
from zipfile import ZipFile

import pytest
from akb.parsing import ParseError, parse_document
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfWriter
from reportlab.pdfgen import canvas


def test_parser_uses_trusted_source_root_from_unrelated_working_directory(tmp_path, monkeypatch):
    from akb.parse_runner import parse_isolated

    # A source-only deployment cannot rely on inherited PYTHONPATH or its cwd.
    # A document directory must not be able to shadow the parser package either.
    poison = tmp_path / "akb"
    poison.mkdir()
    (poison / "__init__.py").write_text("raise RuntimeError('untrusted directory imported')")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    parsed = parse_isolated("synthetic.txt", "来源隔离验证".encode())
    assert parsed.blocks[0].text == "来源隔离验证"
    from pathlib import Path
    Path("relative.txt").write_text("相对路径原件")
    assert parse_isolated("relative.txt", Path("relative.txt")).blocks[0].text == "相对路径原件"


def test_office_and_pdf_locators(tmp_path):
    doc = Document()
    doc.add_paragraph("姓名：陈舟")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "项目", "资料库"
    stream = BytesIO()
    doc.save(stream)
    parsed = parse_document("resume.docx", stream.getvalue())
    assert parsed.blocks[0].locator["paragraph"] == 1
    assert parsed.blocks[1].locator["table"] == 1 and parsed.coverage == "partial"
    workbook = Workbook()
    workbook.active.title = "论文"
    workbook.active.append(["论文名", "发表年份"])
    workbook.active.append(["示例论文", "=2024+1"])
    stream = BytesIO()
    workbook.save(stream)
    parsed = parse_document("papers.xlsx", stream.getvalue())
    assert parsed.blocks[1].locator["range"] == "A2:B2"
    assert "=2024+1" in parsed.blocks[1].text
    object_path = tmp_path / "original"
    object_path.write_bytes(stream.getvalue())
    from akb.parse_runner import parse_isolated

    assert "=2024+1" in parse_isolated("papers.xlsx", object_path).blocks[1].text
    ppt = Presentation()
    slide = ppt.slides.add_slide(ppt.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(2)).text = "项目介绍"
    stream = BytesIO()
    ppt.save(stream)
    assert parse_document("project.pptx", stream.getvalue()).blocks[0].locator["slide"] == 1
    stream = BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(40, 700, "Evidence backed document")
    pdf.save()
    parsed = parse_document("paper.pdf", stream.getvalue())
    assert parsed.blocks[0].locator["page"] == 1 and "Evidence backed" in parsed.blocks[0].text


def test_encrypted_pdf_and_empty_scans():
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.encrypt("secret")
    stream = BytesIO()
    writer.write(stream)
    with pytest.raises(ParseError, match="PDF 已加密"):
        parse_document("encrypted.pdf", stream.getvalue())
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    stream = BytesIO()
    writer.write(stream)
    with pytest.raises(ParseError, match="未提取到文本"):
        parse_document("scan.pdf", stream.getvalue())


def test_html_is_plain_text_and_invalid_office_is_rejected():
    parsed = parse_document("page.html", b"<p>Public text</p><script>private code</script>")
    assert "private code" not in "".join(b.text for b in parsed.blocks)
    with pytest.raises(ParseError, match="Office"):
        parse_document("fake.docx", b"plain text")


def without_dimensions(coordinate="B2"):
    workbook = Workbook()
    workbook.active[coordinate] = "evidence"
    original = BytesIO()
    workbook.save(original)
    output = BytesIO()
    with ZipFile(original) as src, ZipFile(output, "w") as dst:
        for info in src.infolist():
            content = src.read(info.filename)
            if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml"):
                content = re.sub(rb"<dimension\b[^>]*/>", b"", content)
            dst.writestr(info, content)
    return output.getvalue()


def test_xlsx_without_optional_dimension_preserves_sparse_cell_position():
    parsed = parse_document("export.xlsx", without_dimensions())
    assert len(parsed.blocks) == 1
    assert parsed.blocks[0].text == "B2: evidence"
    assert parsed.blocks[0].locator["range"] == "A2:B2"


@pytest.mark.parametrize("coordinate", ["GS1", "A20001"])
def test_xlsx_missing_dimension_still_enforces_bounds(coordinate):
    with pytest.raises(ParseError) as error:
        parse_document("large.xlsx", without_dimensions(coordinate))
    assert error.value.code == "cell_limit"


def test_xlsx_missing_dimension_and_implicit_cell_coordinates():
    workbook = Workbook()
    workbook.active.append(["one", "two", "three"])
    original, output = BytesIO(), BytesIO()
    workbook.save(original)
    with ZipFile(original) as src, ZipFile(output, "w") as dst:
        for info in src.infolist():
            content = src.read(info.filename)
            if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml"):
                content = re.sub(rb"<dimension\b[^>]*/>", b"", content)
                content = re.sub(rb'(<c) r="[A-Z]+[0-9]+"', rb"\1", content)
            dst.writestr(info, content)
    result = parse_document("implicit.xlsx", output.getvalue())
    assert result.blocks[0].text == "A1: one | B1: two | C1: three"
