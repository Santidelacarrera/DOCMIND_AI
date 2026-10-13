"""Deterministic minimal .docx writer for synthetic test and demo documents (no dependency)."""

import io
import zipfile
from xml.sax.saxutils import escape

_CT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    "</Relationships>"
)
_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_FIXED = (2026, 1, 1, 0, 0, 0)  # constant timestamps: byte-for-byte reproducible output


def _run(text: str, hidden: bool = False) -> str:
    props = "<w:rPr><w:vanish/></w:rPr>" if hidden else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def docx_bytes(pages: list[list[str]], *, author: str = "", hidden: list[str] | None = None,
               tables: dict[int, list[list[str]]] | None = None, extra: dict[str, bytes] | None = None,
               extra_content_types: str = "") -> bytes:
    """``pages`` -> a .docx with one explicit page break between pages. ``tables`` maps a page
    index to rows of cells; ``hidden`` adds ``w:vanish`` text; ``extra`` adds raw parts (tests)."""
    body: list[str] = []
    for index, lines in enumerate(pages):
        for line in lines:
            body.append(f"<w:p>{_run(line)}</w:p>")
        for row in (tables or {}).get(index, []):
            cells = "".join(f"<w:tc><w:p>{_run(c)}</w:p></w:tc>" for c in row)
            body.append(f"<w:tbl><w:tr>{cells}</w:tr></w:tbl>")
        if index < len(pages) - 1:
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    for text in hidden or []:
        body.append(f"<w:p>{_run(text, hidden=True)}</w:p>")
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_NS}><w:body>{"".join(body)}</w:body></w:document>'
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:creator>{escape(author)}</dc:creator></cp:coreProperties>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        parts: dict[str, str | bytes] = {
            "[Content_Types].xml": _CT.replace("</Types>", extra_content_types + "</Types>"),
            "_rels/.rels": _RELS, "word/document.xml": document, "docProps/core.xml": core,
        }
        for name, data in {**parts, **(extra or {})}.items():
            info = zipfile.ZipInfo(name, _FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data if isinstance(data, bytes) else data.encode("utf-8"))
    return out.getvalue()
