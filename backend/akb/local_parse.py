"""Time-bounded child parser for the local index; receives a private snapshot only."""

import contextlib
import json
import sys
from pathlib import Path

from akb.local_index import MAX_TEXT
from akb.parsing import ParseError, parse_document


def main():
    path, name, ocr = sys.argv[1:]
    try:
        if (
            Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
            and ocr != "1"
        ):
            raise ParseError("ocr_deferred", "图像正文待按需 OCR；可启用本机 OCR 后重新扫描")
        # Parsers run out of process with a parent wall timeout. Apply address-space
        # bounds where supported (macOS may reject RLIMIT_AS; never change host settings).
        import resource

        try:
            resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        except (ValueError, OSError):
            pass
        with contextlib.redirect_stdout(sys.stderr):
            parsed = parse_document(name, Path(path), ocr_page_limit=3 if ocr == "1" else 0)
        blocks, total = [], 0
        for block in parsed.blocks:
            if total + len(block.text) > MAX_TEXT:
                parsed.coverage, parsed.warning = "partial", "达到每份 50 万字符本机索引预算"
                break
            blocks.append({"text": block.text, "locator": block.locator})
            total += len(block.text)
        data = {"status": "indexed", "blocks": blocks, "coverage": parsed.coverage, "warning": parsed.warning}
    except ParseError as exc:
        data = {
            "status": "deferred"
            if exc.code
            in {"unsupported", "ocr_deferred", "no_text", "password_required", "page_limit", "cell_limit"}
            else "failed",
            "blocks": [],
            "coverage": "none",
            "warning": exc.message,
        }
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
