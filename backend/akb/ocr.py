"""Local CPU OCR; never submits documents or fetches images from a URL."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image

from akb.parsing import ParseError


@lru_cache(maxsize=1)
def engine():
    from rapidocr import RapidOCR

    return RapidOCR(
        params={
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_dml": False,
            "EngineConfig.onnxruntime.use_cann": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )


def recognize(image):
    if image.width * image.height > 40_000_000:
        raise ParseError("pixel_limit", "图片超过 4000 万像素，请缩小后上传")
    import contextlib
    import sys

    with contextlib.redirect_stdout(sys.stderr):
        result = engine()(image.convert("RGB"))
    if not result.txts:
        return []
    return [
        (
            text,
            {
                "line": index + 1,
                "confidence": round(float(score), 4),
                "bbox": [[round(float(c), 2) for c in point] for point in box],
            },
        )
        for index, (text, score, box) in enumerate(zip(result.txts, result.scores, result.boxes, strict=True))
    ]


def ocr_image(data):
    try:
        Image.MAX_IMAGE_PIXELS = 40_000_000
        with Image.open(data if isinstance(data, Path) else BytesIO(data)) as image:
            return recognize(image)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("invalid_image", "图片无法识别，请检查格式、尺寸或清晰度") from exc


def ocr_pdf_page(data, index):
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(data) if isinstance(data, Path) else data) as pdf:
        page = pdf[index]
        try:
            width, height = page.get_size()
            scale = min(2.0, (20_000_000 / max(1, width * height)) ** 0.5)
            bitmap = page.render(scale=scale)
            try:
                return recognize(bitmap.to_pil())
            finally:
                bitmap.close()
        finally:
            page.close()
