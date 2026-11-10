# Evaluation report — `baseline-rules`

22 scored documents of 28 (Tesseract 5.5.0).

## Field extraction (before any human review)

Micro precision **97.6%**, recall **88.9%**, F1 **93.0%** (macro F1 95.1%).

| Field | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| applicant_name | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| counterparty | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| country | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| currency | 11 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| customer_name | 9 | 0 | 2 | 100.0% | 81.8% | 90.0% |
| date_of_birth | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| effective_date | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| email | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| entidad | 4 | 1 | 1 | 80.0% | 80.0% | 80.0% |
| fecha | 5 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| governing_law | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| id_number | 2 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| invoice_date | 7 | 1 | 3 | 87.5% | 70.0% | 77.8% |
| invoice_number | 7 | 0 | 3 | 100.0% | 70.0% | 82.3% |
| monto | 5 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| nombre | 5 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| numero_documento | 5 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| rut | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| tax_amount | 6 | 0 | 2 | 100.0% | 75.0% | 85.7% |
| term_months | 4 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| tipo_documento | 5 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| total_amount | 8 | 0 | 2 | 100.0% | 80.0% | 88.9% |
| total_fee | 3 | 0 | 1 | 100.0% | 75.0% | 85.7% |
| vendor_name | 10 | 1 | 1 | 90.9% | 90.9% | 90.9% |

| Document kind | Precision | Recall | F1 |
|---|---:|---:|---:|
| adversarial | 100.0% | 100.0% | 100.0% |
| digital | 98.7% | 91.2% | 94.8% |
| multipage | 100.0% | 93.3% | 96.5% |
| scanned | 92.9% | 78.8% | 85.2% |

## Where errors come from

Of 142 annotated fields: 127 correct, 0 breaking their schema or checksum (validation), 0 right value in a non-canonical form (normalization), 7 wrong only because the text was damaged (OCR / text layer), 8 wrong even on perfect text (extraction).

OCR: mean character error rate **5.2%**, word error rate **20.8%**.

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
| cl_factura_01 | text_layer | 0.0% | 0.0% |
| cl_boleta_02 | text_layer | 0.0% | 0.0% |
| cl_nota_credito_03 | text_layer | 0.0% | 0.0% |
| cl_factura_rut_invalido_04 | text_layer | 0.0% | 0.0% |
| cl_factura_scan_05 | ocr | 0.5% | 4.0% |

## Confidence and human review

Expected calibration error **0.1289**.

| Confidence | Fields | Mean confidence | Accuracy |
|---|---:|---:|---:|
| 0.0–0.5 | 19 | 0.0 | 36.8% |
| 0.5–0.7 | 11 | 0.6 | 81.8% |
| 0.7–0.9 | 19 | 0.7868 | 94.7% |
| 0.9–1.0 | 93 | 0.9371 | 100.0% |

Routing to a person catches **100.0%** of wrong fields at a review load of **33.1%** of all fields. Accuracy without review 89.4%; accuracy of the auto-accepted fields 100.0%. Wrong fields that were *not* flagged: none.

### Textual evidence vs correctness

A value is `exact`/`normalized` when it was found in the extracted text; `not_found` when it was not. This is not a probability: it is the share of values in each group that match the reference.

| Evidence status | Fields | Correct | Wrong | Precision | Flagged for review |
|---|---:|---:|---:|---:|---:|
| exact | 85 | 83 | 2 | 97.7% | 19 |
| normalized | 34 | 33 | 1 | 97.1% | 5 |
| not_applicable | 19 | 7 | 12 | 36.8% | 19 |
| not_found | 4 | 4 | 0 | 100.0% | 4 |

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

15 documents entered the review queue; 47 fields were reviewed (15 corrected, 32 confirmed). Field accuracy 89.4% → **100.0%** after review (reviewer: simulated (reference annotation used as oracle)).

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
| inv_ambiguous_date_01 | invoice_date | `2025-05-04` | `2025-04-05` | 0.60 | wrong_value |
| contract_long_30p | total_fee | `None` | `90000.0` | 0.00 | missing_value |
| cl_factura_scan_05 | entidad | `Ferreteria Eiemplo Centro SpA` | `Ferreteria Ejemplo Centro SpA` | 0.60 | ocr_misread |

Reasons: confirmed ×32, missing_value ×7, ocr_misread ×7, wrong_value ×1

## Time and cost (machine-dependent)

Mean **0.4696 s** per document end to end (p50 0.1265 s, p95 1.4246 s); digital 0.1352 s, scanned/OCR 1.6065 s.

Approximate LLM cost per document: **$0.00012** (9968 prompt + 1918 completion tokens in total). Projected from token counts at the configured per-1M-token prices (OPENAI_INPUT_PRICE_PER_1M / OPENAI_OUTPUT_PRICE_PER_1M); a rule-based provider actually costs nothing. Token counts are character-based estimates unless the provider reports usage.
