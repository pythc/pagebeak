"""Fill explicit Word placeholders from one current draft; preserve all other template parts."""

import re
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import HTTPException
from lxml import etree

from akb.parsing import ParseError, _zip_check

NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PLACEHOLDER = re.compile(r"\{\{field:([^{}\r\n]{1,80})\}\}")


def fill_template(data, answers):
    try:
        _zip_check(data)
    except ParseError as exc:
        raise HTTPException(422, exc.message) from exc
    values = {a["key"]: a["value"] if a["status"] == "ready" else "" for a in answers}
    output = BytesIO()
    count = 0
    with ZipFile(BytesIO(data)) as original, ZipFile(output, "w", ZIP_DEFLATED) as filled:
        if "word/document.xml" not in original.namelist():
            raise HTTPException(422, "请上传有效的 DOCX 模板")
        for info in original.infolist():
            content = original.read(info.filename)
            if re.fullmatch(r"word/(document|header\d+|footer\d+)\.xml", info.filename):
                try:
                    root = etree.fromstring(content, etree.XMLParser(resolve_entities=False, no_network=True))
                except etree.XMLSyntaxError as exc:
                    raise HTTPException(422, "Word 模板 XML 无法读取") from exc
                changed = False
                for paragraph in root.iter(NS + "p"):
                    nodes = [
                        t
                        for t in paragraph.iter(NS + "t")
                        if next(t.iterancestors(NS + "p"), None) is paragraph
                    ]
                    text = "".join(t.text or "" for t in nodes)
                    for match in reversed(list(PLACEHOLDER.finditer(text))):
                        key = match[1]
                        if key not in values:
                            raise HTTPException(422, f"模板字段 {key} 未包含在该填写草稿中")
                        position = 0
                        for node in nodes:
                            value = node.text or ""
                            end = position + len(value)
                            if end > match.start() and position < match.end():
                                prefix = value[: max(0, match.start() - position)]
                                suffix = value[max(0, match.end() - position) :]
                                insertion = (values[key] or "") if position <= match.start() < end else ""
                                node.text = prefix + insertion + suffix
                                node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                            position = end
                        count += 1
                        changed = True
                if changed:
                    content = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            filled.writestr(info, content)
    if not count:
        raise HTTPException(422, "模板没有 {{field:字段key}} 占位符；请先标记需要填写的位置")
    return output.getvalue()
