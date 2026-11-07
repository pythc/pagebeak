from akb.parse_runner import parse_isolated
from PIL import Image, ImageDraw, ImageFont


def test_real_local_ocr_image_and_scanned_pdf(tmp_path):
    image = Image.new("RGB", (1400, 250), "white")
    ImageDraw.Draw(image).text(
        (60, 70), "Evidence backed Knowledge Library 2026", fill="black", font=ImageFont.load_default(size=45)
    )
    image_path = tmp_path / "sample.png"
    pdf_path = tmp_path / "sample.pdf"
    image.save(image_path)
    image.save(pdf_path, "PDF", resolution=150)
    for path in [image_path, pdf_path]:
        parsed = parse_isolated(path.name, path)
        assert "knowledge" in " ".join(b.text for b in parsed.blocks).casefold()
        assert parsed.coverage == "partial"
        assert all(b.locator["method"] == "ocr" and b.locator["confidence"] > 0.5 for b in parsed.blocks)
        assert all(len(b.locator["bbox"]) == 4 for b in parsed.blocks)
        if path.suffix == ".pdf":
            assert all(b.locator["page"] == 1 for b in parsed.blocks)
