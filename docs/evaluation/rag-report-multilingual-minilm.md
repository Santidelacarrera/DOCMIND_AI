# RAG evaluation report

Embedder `local:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (lexical hashing unless stated otherwise), top_k=5, min_coverage=0.5, 6 fictitious documents in 2 tenants, 28 questions (20 answerable, 8 that must abstain).

Reproduce: `cd backend && python -m app.evaluation.rag --output-dir eval_reports`.

## Metrics

| Metric | Value |
|---|---:|
| `recall@1` | 1.0 |
| `precision@1` | 1.0 |
| `recall@3` | 1.0 |
| `precision@3` | 0.3333 |
| `recall@5` | 1.0 |
| `precision@5` | 0.2 |
| `mrr` | 1.0 |
| `context_relevance` | 0.525 |
| `fact_accuracy` | 0.85 |
| `citation_accuracy` | 0.85 |
| `citation_precision` | 1.0 |
| `faithfulness` | 1.0 |
| `false_abstention_rate` | 0.15 |
| `abstention_accuracy` | 1.0 |
| `cross_tenant_leaks` | 0 |

## By category

| Category | Correct | Questions |
|---|---:|---:|
| cross_tenant | 5 | 5 |
| factual | 16 | 16 |
| paraphrase | 1 | 4 |
| unanswerable | 3 | 3 |

## Failures

* **aurora / paraphrase** — ¿Cuánto cuesta en total el servicio contratado? -> abstained; expected contrato_servicios.pdf, p.3, cited —
* **aurora / paraphrase** — ¿Cuánto tiempo dura el acuerdo con el cliente? -> abstained; expected contrato_servicios.pdf, p.2, cited —
* **aurora / paraphrase** — ¿Con qué frecuencia se hacen los backups de los servidores? -> abstained; expected manual_seguridad.pdf, p.3, cited —

## Reading these numbers

* The hashing embedder matches words, not meaning: paraphrases with no shared terms are expected to miss. That is a limitation of the offline default, measured here on purpose.
* `answer()` is extractive: it quotes masked sentences and never generates text, so `faithfulness` checks that each quoted excerpt is literally in the cited chunk.
* `cross_tenant_leaks` counts any retrieved chunk or citation from another organization; the gate requires 0.
