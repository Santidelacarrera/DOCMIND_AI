# ADR-003: Evidencia semántica en `/ask`, con umbral calibrado por modelo

**Fecha:** 2026-11-10 · **Estado:** aceptada · **Relacionado:** ADR-002, `docs/DECISIONS_2026-11-10.md`

## Contexto

ADR-002 exige que la frase citada cubra ≥ 0,5 de los términos de la pregunta. Es léxico: con cualquier embedder, las paráfrasis sin palabras comunes se abstenían (1/4 correctas) aunque el fragmento correcto se recuperase en primera posición.

## Opciones evaluadas

| Opción | Resultado medido (28 preguntas) |
|---|---|
| Solo regla léxica (ADR-002) | 24/28; paráfrasis 1/4; abstención 1,0 |
| Selección semántica con umbral 0,5 para cualquier embedder semántico | Multilingüe: 28/28. **`bge-small-en`: abstención 0,0** (puntúa por encima de 0,5 frases españolas no relacionadas) |
| **Selección semántica solo con umbral calibrado para el modelo** (elegida) | Multilingüe: 28/28, abstención 1,0. `bge-small-en` vuelve a la regla léxica: 24/28, abstención 1,0 |

## Decisión

* Con un embedder semántico (`semantic = True`) **y** un umbral (`ASK_MIN_SEMANTIC_SIMILARITY` o, si está vacío, `CALIBRATED_SIMILARITY[modelo]`), las frases se ordenan por similitud coseno con la pregunta y hay evidencia suficiente si la mejor cubre ≥ `ASK_MIN_EVIDENCE_COVERAGE` **o** su similitud es ≥ el umbral.
* Sin umbral calibrado, se aplica solo la regla léxica. `HashingEmbedder` nunca toma el camino semántico.
* Calibrado hoy: `local:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` → 0,5.
* La pregunta se embebe una sola vez (`_retrieve` devuelve el vector).

## Consecuencias

* Un modelo nuevo no responde a paráfrasis hasta medirlo con `python -m app.evaluation.rag --embedder local --model <m> --min-similarity <t>` y añadirlo a `CALIBRATED_SIMILARITY`. Es deliberado: un umbral de otro modelo puede anular la abstención.
* Coste: un embedding por frase candidata (pocas decenas por pregunta). Con un embedder externo, esas frases (enmascaradas, ya enviadas al indexar) vuelven a enviarse.
* Margen estrecho en el corpus (0,44 frente a 0,52): re-medir con documentos reales.
