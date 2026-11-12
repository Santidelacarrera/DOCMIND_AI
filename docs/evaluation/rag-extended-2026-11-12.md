# Evaluación RAG ampliada — 2026-11-12

Corpus `backend/eval_corpus/rag_questions_extended.json`: 8 documentos ficticios en 2 organizaciones y 12 preguntas.

| Categoría | Preguntas |
|---|---:|
| Identificadores (`ZX-4471`) | 3 |
| Tabla | 2 |
| Documento de 12 páginas, el dato en la 9 | 1 |
| Dos actas que se contradicen | 1 |
| Documento con inyección («Ignora todas las instrucciones…») | 1 |
| Sin respuesta | 2 |
| De otra organización | 2 |

Reproducir:

```bash
cd backend
python -m app.evaluation.rag --corpus extended --retrieval dense|hybrid [--answer-mode generative]
EMBEDDING_LOCAL_FILES_ONLY=false python -m app.evaluation.rag --corpus extended --embedder local \
  --model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 --retrieval dense|hybrid
```

## Resultados (después de los arreglos de esta entrega)

| Métrica | hashing · dense | hashing · hybrid | hashing · hybrid · generativa* | MiniLM · dense | MiniLM · hybrid |
|---|---:|---:|---:|---:|---:|
| recall@1 | 0.625 | **1.00** | **1.00** | 1.00 | 1.00 |
| mrr | 0.7604 | **1.00** | **1.00** | 1.00 | 1.00 |
| fact_accuracy | 0.875 | 0.875 | **1.00** | 1.00 | 1.00 |
| citation_accuracy | 0.875 | 0.875 | **1.00** | 1.00 | 1.00 |
| faithfulness | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| abstention_accuracy | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| conflict_detection | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| injection_documents_used | 0 | 0 | 0 | 0 | 0 |
| cross_tenant_leaks | 0 | 0 | 0 | 0 | 0 |

\* `--answer-mode generative` usa el generador sin conexión (`ExtractiveGenerator`). Mide la cadena de verificación, el umbral de términos nombrados y las citas renumeradas, **no un modelo de lenguaje real**. La validación con un LLM real está bloqueada por credenciales.

## Qué encontró la medición y qué se corrigió

| Hallazgo | Antes | Corrección | Después |
|---|---|---|---|
| Pregunta por un identificador inexistente (`ZZ-0000`) o de otra organización (`RT-5512`): el modo extractivo citaba otro expediente | abstention_accuracy 0.50 | Umbral de términos nombrados (`app/rag/entities.py`) también en modo extractivo | 1.00. Sin cambios en el corpus base |
| Las actas contradictorias no se detectaban: la extracción del PDF une el encabezado en mayúsculas («ACTA DE REUNIÓN DE MARZO La audiencia…») y el detector tomaba «MARZO» y «ABRIL» como sujetos distintos | conflict_detection 0.00 | `conflicts.strip_heading` | 1.00 |
| Identificadores con el embedder léxico | recall@1 0.625 (dense) | — | 1.00 con `hybrid` |

## Limitaciones honestas

* Son 12 preguntas, y cada una pesa mucho. No se ajustaron umbrales al corpus. Las dos correcciones tienen sus propias pruebas unitarias con frases distintas de las del corpus.
* Con el modelo multilingüe, `dense` ya acierta todo en ambos corpus, así que `RETRIEVAL_MODE` sigue en `dense` por defecto. `hybrid` se recomienda con el embedder `hashing` y para colecciones con muchos identificadores, a confirmar con documentos reales.
* `QK-2290` («¿Qué asunto tiene…?») se abstiene en modo extractivo con `hashing`: la frase dice «corresponde al arrendamiento» y no contiene «asunto». Es el límite conocido de la regla léxica de evidencia suficiente (ADR-002).
