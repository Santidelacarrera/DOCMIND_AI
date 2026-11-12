# Recuperación híbrida frente a vectorial — 2026-11-12

Medición que fijó el valor por defecto `RETRIEVAL_MODE=dense` (ADR-005). Corpus `backend/eval_corpus/rag_questions.json`: 6 documentos ficticios, 2 organizaciones, 28 preguntas (20 con respuesta, 8 que deben abstenerse). `top_k=5`, `min_coverage=0.5`.

Reproducir:

```bash
cd backend
python -m app.evaluation.rag --retrieval dense
python -m app.evaluation.rag --retrieval hybrid
EMBEDDING_LOCAL_FILES_ONLY=false python -m app.evaluation.rag --embedder local \
  --model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 --retrieval hybrid
```

## Resultados

| Métrica | hashing · dense | hashing · hybrid | MiniLM multilingüe · dense | MiniLM multilingüe · hybrid |
|---|---:|---:|---:|---:|
| recall@1 | 0.90 | **0.95** | **1.00** | 0.95 |
| recall@5 | 0.95 | 0.95 | 1.00 | 1.00 |
| mrr | 0.9167 | **0.95** | **1.00** | 0.975 |
| context_relevance | 0.4217 | **0.5117** | 0.525 | **0.575** |
| fact_accuracy | 0.85 | 0.85 | 1.00 | 1.00 |
| citation_accuracy | 0.85 | 0.85 | 1.00 | 1.00 |
| citation_precision | 1.00 | 1.00 | 0.975 | 0.975 |
| faithfulness | 1.00 | 1.00 | 1.00 | 1.00 |
| abstention_accuracy | 1.00 | 1.00 | 1.00 | 1.00 |
| cross_tenant_leaks | 0 | 0 | 0 | 0 |

En el modelo multilingüe, la única pregunta que baja de posición con `hybrid` es «¿Cuánto tiempo dura el acuerdo con el cliente?» (paráfrasis sin palabras en común con el documento): el fragmento correcto pasa del puesto 1 al 2, y la respuesta y su cita siguen siendo correctas.

## Lectura

* Con el embedder léxico de desarrollo (`hashing`), `hybrid` mejora el primer resultado y la relevancia del contexto sin empeorar nada.
* Con el modelo por defecto de producción (MiniLM multilingüe), `dense` ya es perfecto en este corpus y `hybrid` no mejora la respuesta; solo empeora `recall@1` en una paráfrasis.
* El corpus no contiene preguntas por identificadores exactos (números de expediente, RUT, códigos de factura), que es donde BM25 aporta más. La prueba `test_hybrid_finds_the_exact_term_dense_search_cannot_rank` demuestra el mecanismo, no su efecto en documentos reales.
* Son 28 preguntas: una pregunta equivale a 0,05 en `recall@1`. No se ajustaron pesos ni umbrales a este corpus.

**Decisión:** `dense` sigue siendo el valor por defecto. Activar `hybrid` requiere medirlo con documentos reales anonimizados que incluyan preguntas por identificadores (pendiente del piloto, ver `docs/UNIFICATION_PROGRESS.md`).
