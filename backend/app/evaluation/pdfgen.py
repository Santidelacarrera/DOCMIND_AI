"""A tiny deterministic PDF writer (text pages) and a scanned-page (image-only) writer.

Used only to build the reference corpus. No third-party PDF library is required for the
digital documents, and output is byte-for-byte reproducible (no timestamps or random ids).
"""

import io
import random

_PAGE_W, _PAGE_H = 612, 792
_LEFT, _TOP, _LEADING = 56, 740, 18


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def digital_pdf(pages: list[list[str]]) -> bytes:
    """Build a text PDF (Helvetica, WinAnsi) with one content stream per page."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholder, filled once the page tree id is known
    pages_id = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids: list[int] = []
    for lines in pages:
        ops = [b"BT", b"/F1 11 Tf", f"{_LEADING} TL".encode(), f"{_LEFT} {_TOP} Td".encode()]
        for line in lines:
            ops.append(b"(" + _escape(line) + b") Tj T*")
        ops.append(b"ET")
        stream = b"\n".join(ops)
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page = add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {_PAGE_W} {_PAGE_H}] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>".encode()
        )
        kids.append(page)
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    refs = " ".join(f"{k} 0 R" for k in kids)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{refs}] /Count {len(kids)} >>".encode()

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def scanned_pdf(
    pages: list[list[str]], *, dpi: int = 200, rotate: float = 0.0, noise: float = 0.0,
    blur: float = 0.0, scale: float = 1.0, seed: int = 7,
) -> bytes:
    """Render the text to raster images and wrap them in an image-only PDF (no text layer).

    ``rotate`` (degrees), ``noise`` (0-1 pixel-flip probability), ``blur`` (gaussian radius) and
    ``scale`` (<1 downsamples, simulating a low-resolution scan) degrade the page the way
    real scans are degraded. All randomness is seeded.
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rng = random.Random(seed)
    font = ImageFont.load_default(size=int(11 * dpi / 72 * 1.05))
    width, height = int(_PAGE_W * dpi / 72), int(_PAGE_H * dpi / 72)
    images = []
    for lines in pages:
        image = Image.new("L", (width, height), 255)
        draw = ImageDraw.Draw(image)
        y = int(56 * dpi / 72)
        for line in lines:
            draw.text((int(_LEFT * dpi / 72), y), line, fill=0, font=font)
            y += int(_LEADING * dpi / 72)
        if rotate:
            image = image.rotate(rotate, resample=Image.Resampling.BICUBIC, fillcolor=255)
        if blur:
            image = image.filter(ImageFilter.GaussianBlur(blur))
        if scale != 1.0:
            image = image.resize((int(width * scale), int(height * scale)), Image.Resampling.BILINEAR)
        if noise:
            pixels = image.load()
            assert pixels is not None
            for _ in range(int(image.width * image.height * noise)):
                pixels[rng.randrange(image.width), rng.randrange(image.height)] = rng.choice((0, 255))
        images.append(image.convert("1", dither=Image.Dither.NONE))
    out = io.BytesIO()
    images[0].save(out, "PDF", save_all=True, append_images=images[1:], resolution=dpi * scale)
    return out.getvalue()
