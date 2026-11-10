# ADR-002: Evidencia suficiente para responder y vector index en el stack de desarrollo

**Fecha:** 2026-11-10 · **Estado:** aceptada · **Relacionado:** ADR-001, `docs/DOCMIND_ARCHITECTURE_AUDIT.md`

## Contexto

`POST /api/v1/ask` es extractivo: recupera fragmentos del tenant y cita la frase que más términos comparte con la pregunta. Una evaluación nueva (`app/evaluation/rag.py`, 28 preguntas, 2 tenants ficticios) mostró que, ante preguntas cuya respuesta solo existe en otra organización, el sistema respondía con una frase propia que compartía una única palabra genérica. No se filtraban datos ajenos, pero se presentaba como respaldada una afirmación que no lo estaba. La abstención correcta era del 50 %.

Además, el stack de `docker-compose.yml` no podía responder preguntas: no tenía Qdrant, la imagen no instalaba `qdrant-client` y el embedder por defecto necesitaba un modelo ausente.

## Problema

1. ¿Cuándo hay evidencia suficiente para responder?
2. ¿Cómo dejar `/ask` funcionando en el stack de desarrollo sin descargas ni coste?

## Opciones evaluadas

| Opción | A favor | En contra |
|---|---|---|
| Umbral de puntuación vectorial | Simple | Con el embedder léxico la puntuación no separa casos: frases con una palabra común puntúan alto |
| Juez LLM de soporte | Semántico | Coste, red y no determinista; contradice "local, $0" |
| **Cobertura ponderada de la pregunta** (elegida) | Determinista, local, explicable; los términos ausentes de todos los fragmentos pesan más | Más falsas abstenciones en paráfrasis con embedder léxico |
| Para el stack: modelo local (`fastembed`) en la imagen | Semántico | ~130 MB, descarga en build, más superficie |
| **Para el stack: `hashing` + Qdrant en Compose** (elegida) | Sin descargas, reproducible, ya rechazado en staging/producción | No semántico |

## Decisión

* La frase citada debe cubrir al menos `ASK_MIN_EVIDENCE_COVERAGE` (por defecto **0,5**) del peso de los términos de la pregunta. El peso es la rareza entre los fragmentos recuperados, y un término que no aparece en ninguno conserva el peso máximo. Por debajo del umbral, la respuesta se abstiene. Con 0 se recupera el comportamiento anterior.
* 0,5 es un umbral de principio ("la evidencia cubre la mayor parte de lo preguntado"), no ajustado al corpus: las respuestas correctas cubren ≥ 0,46 y las incorrectas ≤ 0,40, y no se eligió 0,45 para no sobreajustar.
* Compose incluye `qdrant/qdrant:v1.20.0`, la imagen instala `.[qdrant]` y `.env.example` usa `EMBEDDING_PROVIDER=hashing`. Producción debe elegir `local` u `openai`.
* Las citas añaden `chunk_index` y `excerpt` (aditivo).

## Consecuencias

* Abstención correcta: 0,5 → 1,0. Preguntas factuales: 16/16 sin cambio. Falsas abstenciones: 0,05 → 0,15 (paráfrasis). Cero fugas entre tenants.
* El umbral se vuelve a validar cuando se mida un embedder semántico: la cobertura es léxica y puede rechazar paráfrasis bien recuperadas.
* La compuerta de CI (`--min-abstention-accuracy 1.0`) impide reintroducir respuestas sin evidencia.

## Revalidación con embedders semánticos (2026-11-10)

Mismas 28 preguntas con `python -m app.evaluation.rag --embedder local --model <m> --min-coverage <c>`:

| Embedder | Umbral | recall@1 | MRR | Respuestas correctas | Abstención correcta |
|---|---|---:|---:|---:|---:|
| hashing-256 | 0 | 0,90 | 0,92 | 0,95 | 0,50 |
| hashing-256 | 0,5 | 0,90 | 0,92 | 0,85 | 1,00 |
| bge-small-en-v1.5 | 0 | 0,80 | 0,86 | 0,95 | 0,50 |
| bge-small-en-v1.5 | 0,5 | 0,80 | 0,86 | 0,85 | 1,00 |
| paraphrase-multilingual-MiniLM-L12-v2 | 0 | 1,00 | 1,00 | 0,95 | 0,50 |
| paraphrase-multilingual-MiniLM-L12-v2 | 0,5 | 1,00 | 1,00 | 0,85 | 1,00 |

Conclusiones: sin el umbral, la abstención cae a 0,5 con los tres embedders, así que la regla es necesaria e independiente del modelo; con él, ningún embedder pierde preguntas factuales. Las paráfrasis fallan en la selección léxica de la frase, no en la recuperación (el multilingüe las recupera todas en primera posición). Se mantiene 0,5. Siguiente paso: puntuar frases semánticamente (deuda D3). Informe: `docs/evaluation/rag-report-multilingual-minilm.md`.
