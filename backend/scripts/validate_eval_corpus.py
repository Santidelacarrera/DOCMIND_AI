"""Check a real-document evaluation corpus before running it (docs/REAL_DOCUMENT_EVALUATION.md).

    python scripts/validate_eval_corpus.py eval_private/corpus.json

Reads only local files and prints only file names, counts and problems: never document text, so its
output can be pasted into a report. Exit code 0 when there is no error (warnings are allowed).

Checks:
* structure: ``tenants``, ``documents`` and ``questions``, with the fields each category needs;
* every document file exists, is a PDF or DOCX, opens, and is not inside the git repository's tracked tree;
* every ``relevant`` page exists in its document;
* each answerable question's ``contains`` text appears on one of its ``relevant`` pages (text layer).
  Pages without a text layer (scans) cannot be checked here and are reported as "needs OCR";
* categories, tenants and ``forbidden`` names are consistent; questions are not duplicated;
* the size targets: 30–50 documents, at least 3 questions per document on average, every format.
"""

import argparse
import io
import json
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ANSWERABLE = {"factual", "paraphrase", "identifier", "table", "long_document", "contradiction", "injection"}
MUST_ABSTAIN = {"unanswerable", "cross_tenant"}


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split())


def _pages(path: Path) -> list[str]:
    content = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("encrypted PDF")
        return [page.extract_text() or "" for page in reader.pages]
    from app.processing import extract_docx_pages

    return extract_docx_pages(content)


def _tracked_by_git(path: Path) -> bool:
    try:
        result = subprocess.run(["git", "ls-files", "--error-unmatch", str(path)], cwd=path.parent,
                                capture_output=True, check=False)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def validate(corpus_path: Path) -> tuple[list[str], list[str], dict[str, object]]:
    errors: list[str] = []
    warnings: list[str] = []
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    base = corpus_path.parent
    for key in ("tenants", "documents", "questions"):
        if key not in corpus:
            errors.append(f"missing top-level key: {key}")
    if errors:
        return errors, warnings, {}
    tenants = set(corpus["tenants"])
    if len(tenants) < 2:
        warnings.append("only one tenant: cross_tenant questions cannot be evaluated")

    pages_of: dict[str, list[str]] = {}
    tenant_of: dict[str, str] = {}
    formats: Counter[str] = Counter()
    needs_ocr = 0
    for i, doc in enumerate(corpus["documents"]):
        name = doc.get("file")
        where = f"documents[{i}] ({name})"
        if not name or doc.get("tenant") not in tenants:
            errors.append(f"{where}: needs 'file' and a 'tenant' listed in 'tenants'")
            continue
        if name in pages_of:
            errors.append(f"{where}: duplicate file name (names identify documents in the questions)")
            continue
        if "path" not in doc:
            errors.append(f"{where}: real documents need 'path' (relative to the corpus file)")
            continue
        file = Path(doc["path"])
        file = file if file.is_absolute() else base / file
        if not file.is_file():
            errors.append(f"{where}: file not found: {doc['path']}")
            continue
        if file.suffix.lower() not in (".pdf", ".docx"):
            errors.append(f"{where}: only .pdf and .docx are supported")
            continue
        if _tracked_by_git(file):
            errors.append(f"{where}: the file is tracked by git; real documents must stay out of the repository")
        try:
            pages = _pages(file)
        except Exception as exc:
            errors.append(f"{where}: cannot be read ({type(exc).__name__})")
            continue
        pages_of[name], tenant_of[name] = pages, doc["tenant"]
        formats["docx" if file.suffix.lower() == ".docx" else "pdf"] += 1
        empty = sum(not p.strip() for p in pages)
        if empty:
            needs_ocr += 1
            formats["pdf with pages without text (OCR)"] += 1

    seen: set[tuple[str, str]] = set()
    categories: Counter[str] = Counter()
    unverifiable = 0
    for i, q in enumerate(corpus["questions"]):
        where = f"questions[{i}]"
        category, tenant, text = q.get("category"), q.get("tenant"), str(q.get("question", "")).strip()
        if category not in ANSWERABLE | MUST_ABSTAIN:
            errors.append(f"{where}: unknown category {category!r}")
            continue
        if tenant not in tenants or len(text) < 3:
            errors.append(f"{where}: needs a 'tenant' from 'tenants' and a 'question'")
            continue
        if (tenant, _normal(text)) in seen:
            errors.append(f"{where}: duplicated question for the same tenant")
        seen.add((tenant, _normal(text)))
        categories[category] += 1
        relevant = q.get("relevant") or []
        if category in MUST_ABSTAIN:
            if relevant or q.get("contains"):
                errors.append(f"{where}: {category} questions have empty 'relevant' and 'contains'")
            continue
        if not relevant or not str(q.get("contains", "")).strip():
            errors.append(f"{where}: answerable questions need 'relevant' [[file, page], ...] and 'contains'")
            continue
        found_text = False
        verifiable = False
        for pair in relevant:
            if not (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[1], int)):
                errors.append(f"{where}: each 'relevant' entry is [file, page]")
                break
            name, page = pair
            if name not in pages_of:
                errors.append(f"{where}: relevant file {name!r} is not in 'documents' (or could not be read)")
                break
            if tenant_of[name] != tenant:
                errors.append(f"{where}: relevant file {name!r} belongs to another tenant")
                break
            if not 1 <= page <= len(pages_of[name]):
                errors.append(f"{where}: {name!r} has {len(pages_of[name])} pages, not page {page}")
                break
            page_text = pages_of[name][page - 1]
            if page_text.strip():
                verifiable = True
                found_text = found_text or _normal(str(q["contains"])) in _normal(page_text)
        else:
            if verifiable and not found_text:
                warnings.append(f"{where}: 'contains' does not appear in the text layer of its relevant pages "
                                "(check the expected answer, or the page number)")
            if not verifiable:
                unverifiable += 1
        if category == "contradiction" and not q.get("expect_conflict"):
            warnings.append(f"{where}: contradiction questions usually set \"expect_conflict\": true")
        if category == "injection":
            for name in q.get("forbidden", []):
                if name not in pages_of:
                    errors.append(f"{where}: forbidden file {name!r} is not in 'documents'")
            if not q.get("forbidden"):
                warnings.append(f"{where}: injection questions should name the malicious document in 'forbidden'")

    docs = len(pages_of)
    if not 30 <= docs <= 50:
        warnings.append(f"{docs} readable documents: the target is 30–50")
    if docs and len(corpus["questions"]) < 3 * docs:
        warnings.append(f"{len(corpus['questions'])} questions for {docs} documents: aim for at least 3 per document")
    for fmt in ("pdf", "docx"):
        if not formats[fmt]:
            warnings.append(f"no {fmt} documents")
    if not needs_ocr:
        warnings.append("no scanned pages: OCR will not be evaluated")
    for category in ("unanswerable", "cross_tenant"):
        if not categories[category]:
            warnings.append(f"no {category} questions: abstention will not be fully evaluated")
    summary = {
        "documents": docs, "pages": sum(len(p) for p in pages_of.values()), "formats": dict(formats),
        "documents_needing_ocr": needs_ocr, "questions": len(corpus["questions"]), "categories": dict(categories),
        "answerable_questions_not_text_verifiable": unverifiable,
    }
    return errors, warnings, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    args = parser.parse_args(argv)
    errors, warnings, summary = validate(args.corpus)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for w in warnings:
        print(f"[warning] {w}")
    for e in errors:
        print(f"[ERROR] {e}")
    print(f"{len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
