from typing import Protocol

from app.core import settings


class OCRProvider(Protocol):
    def extract_pages(self, content: bytes) -> list[str]: ...


class TesseractProvider:
    """Tesseract via pytesseract.

    After :meth:`extract_pages`, ``last_confidences`` holds the mean word confidence of each
    page in [0, 1] (``None`` for a page with no recognisable words) and ``last_low_words`` the
    normalised words of each page read below ``OCR_WORD_REVIEW_THRESHOLD``. Both are attributes
    so ``extract_pages`` keeps the simple ``list[str]`` contract.
    """

    def __init__(self) -> None:
        self.last_confidences: list[float | None] = []
        self.last_low_words: list[set[str]] = []

    def extract_pages(self, content: bytes) -> list[str]:
        import pytesseract
        from pdf2image import convert_from_bytes

        images = convert_from_bytes(
            content, dpi=200, first_page=1, last_page=settings().max_ocr_pages,
            timeout=settings().ocr_timeout_seconds,
        )
        texts: list[str] = []
        self.last_confidences = []
        self.last_low_words = []
        for image in images:
            data = pytesseract.image_to_data(
                image, output_type=pytesseract.Output.DICT, timeout=settings().ocr_timeout_seconds
            )
            text, confidence = assemble(data)
            texts.append(text)
            self.last_confidences.append(confidence)
            self.last_low_words.append(low_confidence_words(data, settings().ocr_word_review_threshold))
        return texts


def assemble(data: dict[str, list[object]]) -> tuple[str, float | None]:
    """Rebuild page text (lines, blank line between paragraphs) and mean word confidence
    from ``image_to_data`` output."""
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    for i, raw in enumerate(data["text"]):
        word = str(raw).strip()
        conf = float(data["conf"][i])  # type: ignore[arg-type]
        if not word or conf < 0:
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))  # type: ignore[call-overload]
        lines.setdefault(key, []).append(word)
        confidences.append(conf / 100.0)
    out: list[str] = []
    previous: tuple[int, int] | None = None
    for (block, par, _line), words in lines.items():
        if previous is not None and previous != (block, par):
            out.append("")
        out.append(" ".join(words))
        previous = (block, par)
    mean = sum(confidences) / len(confidences) if confidences else None
    return "\n".join(out), mean


def normalise_word(word: str) -> str:
    # Curly and angle quotes as escapes: this file stays ASCII (some tests read sources with the
    # platform encoding).
    return word.strip(".,:;()[]\"'\u201c\u201d\u00ab\u00bb").casefold()


def low_confidence_words(data: dict[str, list[object]], threshold: float) -> set[str]:
    """Normalised words Tesseract read below ``threshold`` (0-1), ignoring bare punctuation."""
    low: set[str] = set()
    for i, raw in enumerate(data["text"]):
        word = normalise_word(str(raw).strip())
        conf = float(data["conf"][i])  # type: ignore[arg-type]
        if word and 0 <= conf < threshold * 100 and any(c.isalnum() for c in word):
            low.add(word)
    return low


def needs_ocr(text: str, pages: int) -> bool:
    # A small per-page threshold avoids expensive OCR for normal digital PDFs.
    return pages > 0 and len(text.strip()) < pages * 20
