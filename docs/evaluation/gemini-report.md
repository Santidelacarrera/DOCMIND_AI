# Evaluation report — `gemini`

17 scored documents of 23 (Tesseract 5.5.0).

## Field extraction (before any human review)

Micro precision **96.0%**, recall **96.0%**, F1 **96.0%** (macro F1 95.0%).

| Field | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| applicant_name | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| counterparty | 3 | 1 | 1 | 75.0% | 75.0% | 75.0% |
| country | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| currency | 11 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| customer_name | 11 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| date_of_birth | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| effective_date | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| email | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| governing_law | 2 | 2 | 2 | 50.0% | 50.0% | 50.0% |
| id_number | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| invoice_date | 10 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| invoice_number | 10 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| tax_amount | 8 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| term_months | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| total_amount | 10 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| total_fee | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| vendor_name | 10 | 1 | 1 | 90.9% | 90.9% | 90.9% |

| Document kind | Precision | Recall | F1 |
|---|---:|---:|---:|
| adversarial | 100.0% | 100.0% | 100.0% |
| digital | 100.0% | 100.0% | 100.0% |
| multipage | 86.7% | 86.7% | 86.7% |
| scanned | 92.3% | 92.3% | 92.3% |

## Where errors come from

Of 107 annotated fields: 103 correct, 3 wrong only because the text was damaged (OCR / text layer), 1 wrong even on perfect text (extraction).

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
| inv_table_01 | text_layer | 0.0% | 0.0% |
| inv_ambiguous_date_01 | text_layer | 0.0% | 0.0% |
| inv_missing_required_01 | text_layer | 0.0% | 0.0% |
| contract_long_30p | text_layer | 0.0% | 0.0% |
| inj_invoice_01 | text_layer | 0.0% | 0.0% |

## Confidence and human review

Expected calibration error **0.0678**.

| Confidence | Fields | Mean confidence | Accuracy |
|---|---:|---:|---:|
| 0.0–0.5 | 4 | 0.0 | 100.0% |
| 0.5–0.7 | 0 | – | n/a |
| 0.7–0.9 | 2 | 0.775 | 50.0% |
| 0.9–1.0 | 101 | 0.997 | 97.0% |

Routing to a person catches **25.0%** of wrong fields at a review load of **16.8%** of all fields. Accuracy without review 96.3%; accuracy of the auto-accepted fields 96.6%. Wrong fields that were *not* flagged: contract_digital_01:governing_law, contract_digital_02:counterparty, scan_contract_3p:governing_law.

### Textual evidence vs correctness

A value is `exact`/`normalized` when it was found in the extracted text; `not_found` when it was not. This is not a probability: it is the share of values in each group that match the reference.

| Evidence status | Fields | Correct | Wrong | Precision | Flagged for review |
|---|---:|---:|---:|---:|---:|
| exact | 67 | 64 | 3 | 95.5% | 6 |
| normalized | 31 | 31 | 0 | 100.0% | 5 |
| not_applicable | 6 | 6 | 0 | 100.0% | 4 |
| not_found | 3 | 2 | 1 | 66.7% | 3 |

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

9 documents entered the review queue; 18 fields were reviewed (1 corrected, 17 confirmed). Field accuracy 96.3% → **97.2%** after review (reviewer: simulated (reference annotation used as oracle)).

| Document | Field | Model value | Corrected to | Confidence | Reason |
|---|---|---|---|---:|---|
| scan_invoice_lowres | vendor_name | `Pacific Marine` | `Pacific Marine Supply Co.` | 0.70 | ocr_misread |

Reasons: confirmed ×17, ocr_misread ×1

## Time and cost (machine-dependent)

Mean **7.7421 s** per document end to end (p50 5.9325 s, p95 15.1501 s); digital 6.7015 s, scanned/OCR 11.124 s.

Approximate LLM cost per document: **$0.0007** (14524 prompt + 3016 completion tokens in total). Projected from token counts at the configured per-1M-token prices (OPENAI_INPUT_PRICE_PER_1M / OPENAI_OUTPUT_PRICE_PER_1M); a rule-based provider actually costs nothing. Token counts are character-based estimates unless the provider reports usage.
