# Evaluation report — `baseline-rules`

13 scored documents of 19 (Tesseract 5.3.4).

## Field extraction (before any human review)

Micro precision **98.6%**, recall **85.0%**, F1 **91.3%** (macro F1 94.4%).

| Field | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| applicant_name | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| counterparty | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| country | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| currency | 8 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| customer_name | 6 | 0 | 2 | 100.0% | 75.0% | 85.7% |
| date_of_birth | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| effective_date | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| email | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| governing_law | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| id_number | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| invoice_date | 6 | 0 | 2 | 100.0% | 75.0% | 85.7% |
| invoice_number | 5 | 0 | 3 | 100.0% | 62.5% | 76.9% |
| tax_amount | 5 | 0 | 2 | 100.0% | 71.4% | 83.3% |
| term_months | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| total_amount | 6 | 0 | 2 | 100.0% | 75.0% | 85.7% |
| total_fee | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| vendor_name | 7 | 1 | 1 | 87.5% | 87.5% | 87.5% |

| Document kind | Precision | Recall | F1 |
|---|---:|---:|---:|
| adversarial | 100.0% | 100.0% | 100.0% |
| digital | 100.0% | 83.8% | 91.2% |
| multipage | 100.0% | 100.0% | 100.0% |
| scanned | 95.2% | 76.9% | 85.1% |

## Where errors come from

Of 81 annotated fields: 69 correct, 6 wrong only because the text was damaged (OCR / text layer), 6 wrong even on perfect text (extraction).

OCR: mean character error rate **6.3%**, word error rate **25.0%**.

| Document | Source | CER | WER |
|---|---|---:|---:|
| inv_digital_01 | text_layer | 0.0% | 0.0% |
| inv_digital_02 | text_layer | 0.0% | 0.0% |
| inv_digital_03 | text_layer | 0.0% | 0.0% |
| inv_digital_04 | text_layer | 0.0% | 0.0% |
| contract_digital_01 | text_layer | 0.0% | 0.0% |
| contract_digital_02 | text_layer | 0.0% | 0.0% |
| form_digital_01 | text_layer | 0.0% | 0.0% |
| form_digital_02 | text_layer | 0.0% | 0.0% |
| scan_invoice_clean | ocr | 0.0% | 0.0% |
| scan_invoice_skewed | ocr | 0.8% | 2.7% |
| scan_invoice_lowres | ocr | 24.5% | 97.3% |
| scan_contract_3p | ocr | 0.0% | 0.0% |
| inj_invoice_01 | text_layer | 0.0% | 0.0% |

## Confidence and human review

Expected calibration error **0.0877**.

| Confidence | Fields | Mean confidence | Accuracy |
|---|---:|---:|---:|
| 0.0–0.5 | 12 | 0.0 | 8.3% |
| 0.5–0.7 | 2 | 0.6 | 100.0% |
| 0.7–0.9 | 15 | 0.79 | 93.3% |
| 0.9–1.0 | 52 | 0.9394 | 100.0% |

Routing to a person catches **100.0%** of wrong fields at a review load of **19.8%** of all fields. Accuracy without review 85.2%; accuracy of the auto-accepted fields 100.0%. Wrong fields that were *not* flagged: none.

## Invalid inputs

6/6 malformed files were rejected with the expected stable code.

| File | Stage | Expected | Got | OK |
|---|---|---|---|---|
| mal_truncated | rejected_at_upload | DOCUMENT_INVALID | DOCUMENT_INVALID | yes |
| mal_not_a_pdf | rejected_at_upload | UNSUPPORTED_FILE_TYPE | UNSUPPORTED_FILE_TYPE | yes |
| mal_empty | rejected_at_upload | UNSUPPORTED_FILE_TYPE | UNSUPPORTED_FILE_TYPE | yes |
| mal_header_only | rejected_at_upload | DOCUMENT_INVALID | DOCUMENT_INVALID | yes |
| mal_encrypted | rejected_at_upload | PDF_ENCRYPTED | PDF_ENCRYPTED | yes |
| mal_zero_pages | rejected_at_upload | DOCUMENT_INVALID | DOCUMENT_INVALID | yes |

## Embedded instructions

| Document | Unexpected fields | Values unchanged | Flagged for review |
|---|---|---|---|
| inj_invoice_01 | none | True | False |

## Review, correction and export

5 documents entered the review queue; 16 fields were reviewed (12 corrected, 4 confirmed). Field accuracy 85.2% → **100.0%** after review (reviewer: simulated (reference annotation used as oracle)).

| Document | Field | Model value | Corrected to | Confidence | Reason |
|---|---|---|---|---:|---|
| inv_digital_03 | invoice_number | `None` | `99817` | 0.00 | missing_value |
| inv_digital_03 | tax_amount | `None` | `2080.08` | 0.00 | missing_value |
| inv_digital_04 | invoice_number | `None` | `RE-55012` | 0.00 | missing_value |
| inv_digital_04 | invoice_date | `None` | `2025-01-12` | 0.00 | missing_value |
| inv_digital_04 | customer_name | `None` | `Alpen Foods AG` | 0.00 | missing_value |
| inv_digital_04 | total_amount | `None` | `2310.0` | 0.00 | missing_value |
| scan_invoice_lowres | invoice_number | `None` | `PMS-40817` | 0.00 | ocr_misread |
| scan_invoice_lowres | invoice_date | `None` | `2025-05-30` | 0.00 | ocr_misread |
| scan_invoice_lowres | vendor_name | `7 Pacitic Marino suit: 60:` | `Pacific Marine Supply Co.` | 0.75 | ocr_misread |
| scan_invoice_lowres | customer_name | `None` | `Coastal Charter Boats` | 0.00 | ocr_misread |
| scan_invoice_lowres | total_amount | `None` | `1973.4` | 0.00 | ocr_misread |
| scan_invoice_lowres | tax_amount | `None` | `133.4` | 0.00 | ocr_misread |

Reasons: confirmed ×4, missing_value ×6, ocr_misread ×6

## Time and cost (machine-dependent)

Mean **0.3436 s** per document end to end (p50 0.0456 s, p95 1.0053 s); digital 0.0458 s, scanned/OCR 1.0139 s.

Approximate LLM cost per document: **$8.1e-05** (2544 prompt + 1113 completion tokens in total). Projected from token counts at the configured per-1M-token prices (OPENAI_INPUT_PRICE_PER_1M / OPENAI_OUTPUT_PRICE_PER_1M); a rule-based provider actually costs nothing. Token counts are character-based estimates unless the provider reports usage.
