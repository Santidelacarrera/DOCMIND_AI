# Informe de Evaluación RAG y Pipeline Documental

> **Estado:** Arnés de evaluación reproducible (`app.evaluation` y `app.evaluation.rag`), corpus sintético base (28 preguntas) y extendido (12 preguntas con tablas, documentos largos, contradicciones, inyección y fugas entre tenants) **COMPLETADOS Y MEDIDOS**. La evaluación con 30–50 documentos reales del cliente en `backend/eval_private/` está **BLOQUEADA POR DATOS REALES ANONIMIZADOS** del propietario.

---

## 1. Pipeline evaluado extremo a extremo

El sistema evalúa el mismo pipeline que corre en producción ([`backend/app/ingestion/pipeline.py`](../backend/app/ingestion/pipeline.py), [`backend/app/ingestion/answering.py`](../backend/app/ingestion/answering.py), [`backend/app/rag/`](../backend/app/rag/)):

1. **Extracción y validación multiformato** (`formats.py`, `pdf.py`, `docx.py`): PDF digital, PDF escaneado/mixto, DOCX, TXT, MD, CSV, XLSX, PPTX e imágenes.
2. **OCR selectivo por página y confianza por palabra** (`ocr.py`, `TesseractProvider`): umbral 0,60 por palabra para enrutar a revisión humana.
3. **Normalización, sanitizado y enmascarado de PII** (`text.py`, `pii.py`, `rut.py`, `safety.py`): enmascarado de RUT chileno (módulo 11), emails, teléfonos, tarjetas; rechazo de NUL bytes y contenido activo.
4. **Segmentación y chunking acotado por página** (`chunking.py`): fragmentos trazables con `document_id`, `page_start`, `page_end`, `page_basis`, `chunk_index` y marca `suspicious` ante patrones de prompt injection.
5. **Embeddings e indexación** (`embeddings.py`, `vectorstore.py`): Qdrant con filtros nativos por `tenant_id`, `project_id` y `document_ids`.
6. **Recuperación densa, léxica BM25Plus e híbrida RRF + Reranking** (`lexical.py`, `fusion.py`, `rerank.py`).
7. **Generación extractiva o generativa verificada frase por frase + detección de conflictos y alias** (`generation.py`, `verification.py`, `conflicts.py`, `aliases.py`).

---

## 2. Resultados medidos en esta auditoría (2026-11-16)

### 2.1 Evaluación de extracción estructurada (`python -m app.evaluation --min-f1 0.89`)
- **Alcance medido**: 17 documentos puntuados de 28 en el corpus de referencia (digitales, multipágina, adversariales y facturas/DTE chilenos; 11 escaneados omitidos en host Windows sin binario `tesseract` local, validados en contenedor Docker).
- **Archivos corruptos/maliciosos**: **6/6 rechazados** con el código exacto esperado (`mal_truncated` → `DOCUMENT_INVALID`, `mal_not_a_pdf` → `UNSUPPORTED_FILE_TYPE`, `mal_empty` → `UNSUPPORTED_FILE_TYPE`, `mal_header_only` → `DOCUMENT_INVALID`, `mal_encrypted` → `PDF_ENCRYPTED`, `mal_zero_pages` → `DOCUMENT_INVALID`).
- **Métricas globales antes de revisión humana**:
  - **Micro Precision**: `99.0%`
  - **Micro Recall**: `92.2%`
  - **Micro F1**: `95.4%` (Macro F1: `96.5%`)
  - **ECE (Expected Calibration Error)**: `0.1514`
  - **Captura de errores por cola de revisión**: `100.0%` de los campos erróneos fueron marcados para revisión humana (carga de revisión `26.6%`), alcanzando **100.0% de exactitud tras revisión**.

### 2.2 Evaluación RAG — Configuraciones comparadas (`python -m app.evaluation.rag`)

| Métrica | Corpus Base (28 preg.) `hashing-256` `dense` + `extractive` | Corpus Extendido (12 preg.) `hashing-256` `hybrid` + `extractive` | Corpus Extendido (12 preg.) `local:paraphrase-multilingual-MiniLM-L12-v2` `hybrid` + `extractive` | Corpus Extendido (12 preg.) `hashing-256` `hybrid` + `generative` (offline) |
|---|---:|---:|---:|---:|
| `recall@1` | `0.9000` | `1.0000` | `1.0000` | `1.0000` |
| `precision@1` | `0.9000` | `1.0000` | `1.0000` | `1.0000` |
| `recall@3` | `0.9500` | `1.0000` | `1.0000` | `1.0000` |
| `precision@3` | `0.3167` | `0.3750` | `0.3750` | `0.3750` |
| `recall@5` | `0.9500` | `1.0000` | `1.0000` | `1.0000` |
| `precision@5` | `0.1900` | `0.2250` | `0.2250` | `0.2250` |
| `ndcg@1` | `0.9000` | `1.0000` | `1.0000` | `1.0000` |
| `ndcg@3` | `0.9250` | `1.0000` | `1.0000` | `1.0000` |
| `ndcg@5` | `0.9250` | `1.0000` | `1.0000` | `1.0000` |
| `mrr` | `0.9167` | `1.0000` | `1.0000` | `1.0000` |
| `context_relevance` | `0.4217` | `0.4750` | `0.4750` | `0.4750` |
| `fact_accuracy` | `0.8500` | `0.8750` | `1.0000` | `1.0000` |
| `citation_accuracy` | `0.8500` | `0.8750` | `1.0000` | `1.0000` |
| `citation_precision` | `1.0000` | `1.0000` | `1.0000` | `1.0000` |
| `faithfulness` | `1.0000` | `1.0000` | `1.0000` | `1.0000` |
| `false_abstention_rate` | `0.1500` | `0.1250` | `0.0000` | `0.0000` |
| `abstention_accuracy` | `1.0000` | `1.0000` | `1.0000` | `1.0000` |
| `cross_tenant_leaks` | `0` | `0` | `0` | `0` |
| `conflict_detection` | — | `1.0000` | `1.0000` | `1.0000` |
| `injection_documents_used` | — | `0` | `0` | `0` |
| Latencia `p50` / `p95` | `1.8 ms` / `12.6 ms` | `10.5 ms` / `22.2 ms` | `64.3 ms` / `866.3 ms` | `35.7 ms` / `171.9 ms` |

---

## 3. Procedimiento para el corpus privado de 30–50 documentos reales (`backend/eval_private/`)

El directorio `backend/eval_private/` está excluido en `.gitignore`. Cuando el propietario reúna los 30–50 documentos reales anonimizados:

1. Colocar los archivos en `backend/eval_private/docs/` y el manifiesto en `backend/eval_private/corpus.json` (siguiendo [`docs/evaluation/real_corpus_template.json`](evaluation/real_corpus_template.json)).
2. Validar integridad y ausencia de rastreo Git antes de medir:
   ```bash
   cd backend
   python scripts/validate_eval_corpus.py eval_private/corpus.json
   ```
3. Descargar los modelos ONNX locales y verificar checksums:
   ```bash
   EMBEDDING_CACHE_DIR=/opt/models EMBEDDING_LOCAL_FILES_ONLY=false python scripts/fetch_models.py
   ```
4. Ejecutar la comparativa completa (`dense`, `hybrid` y `rerank`):
   ```bash
   python -m app.evaluation.rag --embedder local --corpus eval_private/corpus.json --retrieval dense --output-dir eval_private/reports/dense
   python -m app.evaluation.rag --embedder local --corpus eval_private/corpus.json --retrieval hybrid --output-dir eval_private/reports/hybrid
   python -m app.evaluation.rag --embedder local --corpus eval_private/corpus.json --retrieval hybrid --rerank-model BAAI/bge-reranker-v2-m3-int8 --output-dir eval_private/reports/rerank
   ```
