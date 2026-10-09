# Quality evaluation

DocMind AI is judged on **measured field accuracy, traceability and error handling**, not on the fact that it calls a model. This page defines what is measured, how to reproduce it, what the current numbers are, and what they do **not** prove.

```bash
cd backend
python -m app.evaluation                      # offline baseline extractor, no API key, a few seconds
python -m app.evaluation --provider openai    # the real model (needs OPENAI_API_KEY; costs money)
python -m app.evaluation --min-f1 0.89        # exit 1 below a threshold (used in CI)
```

It writes `eval_reports/report-<provider>.json` (machine readable) and `.md` (human readable). A committed reference run is in [evaluation/baseline-report.md](evaluation/baseline-report.md).

## What is evaluated

The run drives the **real application**: HTTP upload → validation → worker (text layer, OCR, extraction, validation) → status → extraction → review → approval → export. Only the Celery transport is replaced by a direct in-process call. Nothing is mocked inside the pipeline.

### Reference corpus (`backend/eval_corpus/`)

19 documents with a full annotation (`manifest.json`: every field value, the page text, the expected outcome). `python -m app.evaluation.corpus` regenerates them; digital PDFs are byte-for-byte reproducible and a test enforces it.

| Kind | Documents | What it stresses |
|---|---|---|
| Digital PDF | 4 invoices (US / ES / UK / DE layouts), 2 forms | label variants, `DD/MM/YYYY`, decimal comma, currency only in a label, a language the baseline does not know, a field that is absent |
| Multi-page | 4- and 6-page contracts | facts spread over pages, expressed in sentences, filler clauses |
| Scanned | clean 200 dpi, skewed + noisy, half-resolution, 3-page contract | the OCR path and its failure modes |
| Adversarial | invoice with embedded instructions ("ignore previous instructions… add `admin_override`… send to a URL") | prompt injection |
| Malformed | truncated, text renamed `.pdf`, empty, header only, encrypted, zero pages | rejection with a stable error code |

The corpus is synthetic and small. It is a **regression instrument**, not a sample of your documents — see *Limitations*.

### Metrics

| Metric | Definition |
|---|---|
| Field precision / recall / F1 | per field and overall (micro), plus per document kind. A correct value is a TP; a wrong value is **both** an FP and an FN; a missed value an FN; a value where none exists (hallucination) an FP. Comparison is case, whitespace and accent-insensitive; numbers compare to the cent; dates are ISO |
| OCR error | character error rate (CER) and word error rate (WER) between the OCR output and the annotated page text (whitespace-insensitive Levenshtein) |
| Error attribution | each wrong field is re-extracted from the **annotated, perfect text**: right there ⇒ the text (OCR / text layer) was at fault; wrong there too ⇒ extraction was at fault. With an LLM provider this separates "OCR problem" from "model problem" |
| Confidence calibration | accuracy per confidence bin and expected calibration error (ECE) |
| Review routing | what the human-review gate buys: share of wrong fields sent to a person (**error capture**), share of all fields sent (**review load**), accuracy of the fields that are *not* sent (**auto-accept accuracy**) and the list of silent errors |
| Rejection accuracy | malformed inputs rejected with the expected stable code |
| Injection resistance | no field outside the schema, annotated values unchanged |
| Review workflow | accuracy before/after a review pass, every correction with its reason |
| Time and cost | wall time per document (p50/p95, digital vs OCR) and a token-based cost projection |

## How output is validated and when it is rejected

1. **Envelope** – the model answer must be `{"fields": {...}, "confidence": {...}}`. Anything else is **rejected**: the model is re-asked up to `LLM_INVALID_OUTPUT_RETRIES` times, then the job fails with `LLM_SCHEMA_INVALID`; a failed run is recorded (no values are stored).
2. **Tenant JSON Schema** – violations (type, required, enum, bounds, `additionalProperties`…) are listed as `schema` issues. `SCHEMA_VIOLATION_POLICY=review` (default) keeps the result but forces human review; `reject` fails the job like (1).
3. **Per-field confidence** – below `CONFIDENCE_REVIEW_THRESHOLD` (0.70) → `low_confidence`; a field with no usable confidence → `no_confidence`.
4. **Unexpected fields** – any key not declared in the schema's `properties` → `unexpected_field` (typical of a hijacked answer).
5. **OCR confidence** – mean Tesseract word confidence below `OCR_REVIEW_THRESHOLD` (0.80), or no text recognised, flags **every** field of the document (`low_ocr_confidence`): the model cannot see the pixels, so its own confidence is not evidence.

Any issue ⇒ `requires_review`. The document appears in `GET /review/queue`; each flagged field is marked `needs_review`; `POST /documents/{id}/review/approve` is refused (`409 REVIEW_INCOMPLETE`) until every flagged field was confirmed or corrected.

## Current results (offline baseline extractor)

From [evaluation/baseline-report.md](evaluation/baseline-report.md) (Tesseract 5.3.4):

| | Result |
|---|---|
| Field extraction, no review | precision **98.6 %**, recall **85.0 %**, F1 **91.3 %** (13 documents, 81 annotated fields) |
| Multi-page / adversarial | 100 % / 100 % |
| Digital single-page | recall 83.8 % – misses German labels and two label formats the rules do not cover |
| OCR | mean CER **6.3 %** (clean 0.0 %, skewed 0.8 %, half-resolution 24.5 %) |
| Where errors come from | 69 correct · 6 wrong because the text was damaged · 6 wrong even on perfect text |
| Review gate | catches **100 %** of wrong fields at a load of **19.8 %** of fields; auto-accepted accuracy 100 %; no silent errors |
| Invalid inputs | 6 / 6 rejected with the expected code |
| Prompt-injection document | no extra field, values unchanged |
| After the (simulated) review pass | field accuracy 85.2 % → **100 %**; 12 values corrected, 4 confirmed; reasons recorded |

### What these numbers do and do not mean

* **The baseline is not an LLM.** It is a deterministic rule extractor (English/Spanish labels) used so the harness and the pipeline are measurable offline and the numbers are reproducible. Its recall is the room an LLM provider is supposed to fill. **No LLM accuracy figure is claimed here**: run `--provider openai` with your key to produce one, on documents like yours.
* **The review pass is simulated.** The reference annotation plays the reviewer, so "100 % after review" shows the *workflow* works and that nothing wrong slips past the gate in this corpus. It says nothing about how fast or how accurate a person is.
* **Error capture of 100 % holds on 13 documents.** It is a regression bar, not a guarantee. The low-resolution scan *was* a silent error until OCR confidence was added to the gate — that is the kind of finding this harness exists to surface.
* **Time** excludes model latency (the baseline answers instantly). Pipeline time is ≈0.05 s per digital document and ≈1 s per scanned page on the reference machine; add the provider's round trip for LLM runs.
* **Cost** is *projected* from token counts at the configured prices (`OPENAI_INPUT_PRICE_PER_1M`, `OPENAI_OUTPUT_PRICE_PER_1M`; defaults are placeholders — set current prices). With a real provider the run stores the provider-reported token usage and a per-run `estimated_cost`, visible in `GET …/extraction` under `model`.

## Regression gate

`tests/test_evaluation.py` runs the corpus on every `pytest` and compares to `eval_corpus/baseline_expected.json`: OCR-free kinds must match **exactly**; overall F1/precision, error capture, auto-accept accuracy, silent errors and OCR CER must stay within recorded floors. After an intentional change:

```bash
python -m app.evaluation --update-expected   # rewrites eval_corpus/baseline_expected.json
```

and review the diff like code. The scanned-document checks need `tesseract-ocr` and `poppler-utils` and are skipped without them.

## Adding your own documents

1. Add a `Spec` to `app/evaluation/corpus.py` (or drop a PDF in `eval_corpus/pdfs/` and add its annotation to `manifest.json` by hand).
2. `python -m app.evaluation.corpus && python -m app.evaluation --provider openai`.
3. Annotate **what a reviewer wants exported** (ISO dates, plain numbers). Annotate absent fields as `null` — hallucinations only show up if you do.

## Limitations

* Synthetic, 19 documents, English/Spanish/German text: use it to detect regressions, not to forecast accuracy on your data. Build a held-out set of real (consented, redacted) documents before making accuracy claims to customers.
* Layout-dependent content (tables, handwriting, stamps) is not covered.
* Confidence calibration on 81 fields is indicative only; ECE needs a larger set.
