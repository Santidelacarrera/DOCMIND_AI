"""Tests for multi-format document processing (TXT, MD, CSV, XLSX, PPTX, Images, mixed PDF)."""

import io
import zipfile

import pytest
from openpyxl import Workbook
from PIL import Image
from qdrant_client import QdrantClient

from app import worker
from app.evaluation.pdfgen import digital_pdf
from app.ingestion import formats
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline, IngestionRefused
from app.ingestion.vectorstore import QdrantStore
from app.ocr import TesseractProvider, pages_needing_ocr
from tests.conftest import make_pdf


def _xlsx_bytes(rows_by_sheet: dict[str, list[list[object]]]) -> bytes:
    wb = Workbook()
    first = True
    for name, rows in rows_by_sheet.items():
        ws = wb.active if first else wb.create_sheet(title=name)
        assert ws is not None
        ws.title = name
        first = False
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pptx_bytes(slides: list[str], *, notes: dict[int, str] | None = None, macro: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        ct_extra = (
            '<Override PartName="/ppt/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/>'
            if macro
            else ""
        )
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f"{ct_extra}</Types>",
        )
        zf.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
        for idx, text in enumerate(slides, start=1):
            zf.writestr(
                f"ppt/slides/slide{idx}.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>"
                "</p:sld>",
            )
            if notes and idx in notes:
                zf.writestr(
                    f"ppt/slides/_rels/slide{idx}.xml.rels",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide{idx}.xml"/>'
                    "</Relationships>",
                )
                zf.writestr(
                    f"ppt/notesSlides/notesSlide{idx}.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{notes[idx]}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>"
                    "</p:notes>",
                )
        if macro:
            zf.writestr("ppt/vbaProject.bin", b"MZ-macro")
    return buf.getvalue()


def _png_bytes(size: tuple[int, int] = (80, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_txt_md_csv_xlsx_pptx_detection_and_extraction() -> None:
    txt = b"Contract Agreement\nEffective date: 2026-01-15.\nTotal: USD 5000."
    assert formats.detect_format("policy.txt", txt).mime_type == formats.TXT_MIME
    assert "Effective date: 2026-01-15" in formats.extract_txt(txt).pages[0]

    csv_data = b"item;amount;currency\nConsulting;1500;USD\nSupport;500;USD\n"
    assert formats.detect_format("costs.csv", csv_data).mime_type == formats.CSV_MIME
    csv_ext = formats.extract_csv(csv_data)
    assert csv_ext.tables_detected == 1
    assert "item | amount | currency" in csv_ext.pages[0]
    assert "Consulting | 1500 | USD" in csv_ext.pages[0]

    xlsx = _xlsx_bytes({"Summary": [["Client", "Amount"], ["Acme", 2400]], "Notes": [["Due", "2026-06-01"]]})
    assert formats.detect_format("budget.xlsx", xlsx).mime_type == formats.XLSX_MIME
    xlsx_ext = formats.extract_xlsx(xlsx)
    assert xlsx_ext.tables_detected == 2
    assert any("Acme | 2400" in p for p in xlsx_ext.pages)

    pptx = _pptx_bytes(["Q4 Revenue grew 18%", "Risk Controls"], notes={1: "Verify CFO sign-off"})
    assert formats.detect_format("deck.pptx", pptx).mime_type == formats.PPTX_MIME
    pptx_ext = formats.extract_pptx(pptx)
    assert len(pptx_ext.pages) == 2
    assert "Q4 Revenue grew 18%" in pptx_ext.pages[0]
    assert "[Notes] Verify CFO sign-off" in pptx_ext.pages[0]


def test_magic_byte_mismatches_and_macros_rejected() -> None:
    # PDF bytes disguised as .txt must fail closed
    with pytest.raises(formats.UnsafeFormat, match="UNSUPPORTED_FILE_TYPE"):
        formats.detect_format("fake.txt", make_pdf())
    # PNG bytes disguised as .jpg must fail closed
    with pytest.raises(formats.UnsafeFormat, match="UNSUPPORTED_FILE_TYPE"):
        formats.detect_format("fake.jpg", _png_bytes())
    # Macro-enabled pptx must be rejected
    with pytest.raises(formats.UnsafeFormat, match="ACTIVE_CONTENT_REJECTED"):
        formats.extract_pptx(_pptx_bytes(["Hello"], macro=True))
    # Path traversal inside zip must be rejected
    bad_zip = io.BytesIO()
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../evil.xml", "bad")
    with pytest.raises(formats.UnsafeFormat, match="OOXML_PATH_TRAVERSAL"):
        formats.extract_pptx(bad_zip.getvalue())


def test_running_headers_and_mixed_pdf_ocr_helper() -> None:
    pages = [
        "ACME CONFIDENTIAL HEADER\nPage 1 body content about SLA.\nFooter 2026",
        "ACME CONFIDENTIAL HEADER\nPage 2 body content about pricing.\nFooter 2026",
        "ACME CONFIDENTIAL HEADER\nPage 3 body content about termination.\nFooter 2026",
        "ACME CONFIDENTIAL HEADER\nPage 4 body content about renewal.\nFooter 2026",
    ]
    cleaned = formats.strip_running_headers_footers(pages)
    assert all("ACME CONFIDENTIAL HEADER" not in p for p in cleaned)
    assert all("Footer 2026" not in p for p in cleaned)
    assert "Page 2 body content" in cleaned[1]

    assert pages_needing_ocr(["Digital text on page one is long enough.", "   ", "Another digital page."]) == [2]


def test_pipeline_ingests_csv_pptx_xlsx_and_images(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = "11111111-1111-1111-1111-111111111111"
    project = "22222222-2222-2222-2222-222222222222"
    pipeline = IngestionPipeline(QdrantStore(QdrantClient(":memory:"), "fmt_test"), HashingEmbedder(256))

    r_csv = pipeline.ingest(
        tenant_id=tenant,
        document_id="33333333-3333-3333-3333-333333333331",
        content=b"vendor,amount\nCloudHost,9900\n",
        source_name="spend.csv",
        project_id=project,
    )
    assert r_csv.format == "csv" and r_csv.tables_detected == 1 and r_csv.chunks >= 1

    r_pptx = pipeline.ingest(
        tenant_id=tenant,
        document_id="33333333-3333-3333-3333-333333333332",
        content=_pptx_bytes(["Security audit completed in October 2026."]),
        source_name="audit.pptx",
        project_id=project,
    )
    assert r_pptx.format == "pptx" and r_pptx.pages == 1

    monkeypatch.setattr(TesseractProvider, "extract_images", lambda self, imgs: ["Scanned Receipt Total 450 USD"] * len(imgs))
    r_img = pipeline.ingest(
        tenant_id=tenant,
        document_id="33333333-3333-3333-3333-333333333333",
        content=_png_bytes(),
        source_name="receipt.png",
        project_id=project,
    )
    assert r_img.format == "image" and r_img.ocr_used is True and r_img.chunks >= 1

    # NUL bytes inside .txt refused cleanly
    with pytest.raises(IngestionRefused):
        pipeline.ingest(
            tenant_id=tenant,
            document_id="33333333-3333-3333-3333-333333333334",
            content=b"hello\x00world",
            source_name="bad.txt",
            project_id=project,
        )


def test_api_upload_and_worker_process_all_formats(register, client, monkeypatch: pytest.MonkeyPatch) -> None:
    acct = register("formats")
    project = acct.project("multi")
    monkeypatch.setattr(
        worker.TesseractProvider,
        "extract_images",
        lambda self, imgs: ["Invoice INV-IMG-99 Date: 2026-05-01 Total: USD 320"] * len(imgs),
    )

    samples = [
        ("notes.txt", b"Contract summary\nNotice period: 45 days.\nGoverning law: Chile.\n", "text/plain"),
        ("ledger.csv", b"account,balance\nOperations,12000\n", "text/csv"),
        ("sheet.xlsx", _xlsx_bytes({"Data": [["Partner", "Fee"], ["Northwind", 750]]}), formats.XLSX_MIME),
        ("slides.pptx", _pptx_bytes(["Board resolution approved 2026-08-12"]), formats.PPTX_MIME),
        ("scan.png", _png_bytes(), formats.PNG_MIME),
    ]
    for filename, payload, mime in samples:
        up = client.post(
            f"/api/v1/documents?organization_id={acct.org}&project_id={project}",
            files={"file": (filename, payload, mime)},
            headers=acct.headers,
        )
        assert up.status_code == 202, (filename, up.text)
        worker.process_document.run(up.json()["job_id"])  # type: ignore[attr-defined]
        doc_id = up.json()["document_id"]
        preview = client.get(f"/api/v1/documents/{doc_id}/preview?organization_id={acct.org}", headers=acct.headers)
        assert preview.status_code == 200
        assert preview.json()["pages"], filename

    # Mixed PDF: page 1 digital, page 2 blank -> triggers partial OCR for page 2 only
    mixed = digital_pdf([["Cover page digital text long enough to skip OCR."], [""]])
    monkeypatch.setattr(
        worker.TesseractProvider,
        "extract_pages",
        lambda self, content, page_numbers=None: ["Scanned page 2 recovered via OCR"] * len(page_numbers or [1]),
    )
    up_mixed = acct.upload(project, mixed, "mixed.pdf")
    assert up_mixed.status_code == 202
    worker.process_document.run(up_mixed.json()["job_id"])  # type: ignore[attr-defined]
    prev_mixed = client.get(
        f"/api/v1/documents/{up_mixed.json()['document_id']}/preview?organization_id={acct.org}",
        headers=acct.headers,
    ).json()
    assert "Cover page digital text" in prev_mixed["pages"][0]["text"]
    assert "Scanned page 2 recovered via OCR" in prev_mixed["pages"][1]["text"]
