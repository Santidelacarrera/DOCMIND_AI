from typing import Protocol

from app.core import settings


class OCRProvider(Protocol):
    def extract_pages(self, content: bytes) -> list[str]: ...


class TesseractProvider:
    def extract_pages(self, content: bytes) -> list[str]:
        import pytesseract
        from pdf2image import convert_from_bytes

        images = convert_from_bytes(
            content, dpi=200, first_page=1, last_page=settings().max_ocr_pages,
            timeout=settings().ocr_timeout_seconds,
        )
        return [
            pytesseract.image_to_string(image, timeout=settings().ocr_timeout_seconds)
            for image in images
        ]


def needs_ocr(text: str, pages: int) -> bool:
    # A small per-page threshold avoids expensive OCR for normal digital PDFs.
    return pages > 0 and len(text.strip()) < pages * 20
