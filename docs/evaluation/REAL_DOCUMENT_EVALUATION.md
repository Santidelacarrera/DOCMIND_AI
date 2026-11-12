# Evaluación con documentos reales

**Estado (2026-11-12): PREPARADA, SIN EJECUTAR.** No hay documentos reales: `backend/eval_private/` no existe. Este documento recoge la metodología, cómo preparar los datos y los comandos. Los resultados se añadirán aquí cuando se ejecute. **No contiene ninguna métrica todavía, y ninguna cifra de los corpus ficticios debe leerse como calidad real.**

## 1. Qué hay que preparar (propietario)

| Elemento | Requisito |
|---|---|
| Documentos | 30–50, anonimizados **antes** de salir del cliente: nombres, RUT, cuentas, direcciones, teléfonos y correos sustituidos de forma coherente (el mismo valor, siempre por el mismo sustituto) |
| Variedad | PDF digitales, PDF escaneados, DOCX, al menos 3 documentos de más de 20 páginas, documentos con tablas, con imágenes y con estructura compleja (anexos, varias columnas) |
| Casos difíciles | Dos documentos que se contradigan (por ejemplo dos versiones de un contrato), documentos con identificadores (expedientes, facturas, RUT) y, si se quiere medir la inyección, un documento con instrucciones dirigidas a un modelo (se puede redactar a propósito) |
| Dos clientes | Al menos dos `tenants`. Los documentos del segundo sirven de control de aislamiento: las preguntas `cross_tenant` del cliente B sobre contenido del cliente A deben abstenerse |
| Preguntas | De 3 a 5 por documento (90–150 en total), escritas por alguien que conozca los documentos y **antes** de ver las respuestas del sistema. Al menos un 15 % sin respuesta (`unanswerable`) y algunas `cross_tenant` |
| Respuesta esperada | `relevant`: documento y página donde está la respuesta. `contains`: el fragmento literal, tal como aparece en el documento, que una respuesta correcta debe contener |

Ubicación: `backend/eval_private/` (ignorada por git; comprobado con `git check-ignore`):

```
backend/eval_private/
  corpus.json             # a partir de docs/evaluation/real_corpus_template.json
  docs/cliente_a/*.pdf|docx
  docs/cliente_b/*.pdf|docx
  reports/                # salida de las ejecuciones
```

Formato y categorías: la plantilla `docs/evaluation/real_corpus_template.json` tiene un ejemplo de cada categoría (`factual`, `paraphrase`, `identifier`, `table`, `long_document`, `contradiction`, `injection`, `unanswerable`, `cross_tenant`).

## 2. Validación previa (sin modelos ni coste)

```bash
cd backend
python scripts/validate_eval_corpus.py eval_private/corpus.json
```

Comprueba:
* que los ficheros existen, se abren y **no están en git**;
* que las páginas citadas existen;
* que el `contains` aparece en la capa de texto de su página (detecta respuestas esperadas mal copiadas);
* las categorías, los clientes y los objetivos de tamaño.

Solo imprime nombres y contadores, nunca texto de los documentos. En las páginas escaneadas no se puede verificar el `contains` hasta pasar por el OCR: se informan como «needs OCR».

## 3. Comparación reproducible (mismas preguntas en todas las configuraciones)

Todas las ejecuciones ingieren los documentos con el pipeline real: saneado, extracción de texto, **OCR con Tesseract** en las páginas sin capa de texto, enmascarado de PII, fragmentos por página, embeddings e indexación en Qdrant.

| Id | Recuperación | Reranking | Respuesta | Comando (desde `backend/`, con `--embedder local --corpus eval_private/corpus.json`) | Coste |
|---|---|---|---|---|---|
| A0 | dense | — | extractiva | `--retrieval dense` | 0 |
| A | hybrid | — | extractiva | `--retrieval hybrid` | 0 |
| B | hybrid | bge-reranker-v2-m3-int8 | extractiva | `--retrieval hybrid --rerank-model BAAI/bge-reranker-v2-m3-int8` | 0 (CPU local) |
| C | hybrid | — | generativa con Vertex | `--retrieval hybrid --answer-mode generative --generator vertex` | **de pago** |
| D | hybrid | bge-reranker | generativa con Vertex | `B` + `--answer-mode generative --generator vertex` | **de pago** |
| C0 | hybrid | — | generativa sin conexión | `--retrieval hybrid --answer-mode generative` | 0 (control de C: misma cadena de verificación, sin modelo) |

Añadir `--output-dir eval_private/reports/<Id>` a cada ejecución. Antes de C y D:
* hacer **una** prueba de conectividad (`python scripts/vertex_check.py --yes`);
* estimar el coste: unas `preguntas × 2` llamadas con 1–3 k tokens de entrada cada una. El informe de cada ejecución lo calcula con `GEMINI_*_PRICE_PER_1M`.

## 4. Métricas

Las calcula `python -m app.evaluation.rag` y van en el `rag-report.json` de cada ejecución:

| Grupo | Métricas |
|---|---|
| Recuperación | `recall@1/3/5`, `precision@1/3/5`, `ndcg@1/3/5` (relevancia binaria por documento y página), `mrr`, `context_relevance` |
| Respuestas | `fact_accuracy` (contiene el dato esperado), `citation_accuracy` (la primera cita es la página correcta), `citation_precision`, `faithfulness` (cada extracto citado es texto literal del fragmento citado), `false_abstention_rate` |
| Abstención y seguridad | `abstention_accuracy` (sin respuesta y de otro cliente), `cross_tenant_leaks` (debe ser 0), `injection_documents_used` (debe ser 0), `conflict_detection` |
| Operación | `latency_ms` p50/p95/max por pregunta; `llm_usage` (llamadas, tokens, coste estimado) en las ejecuciones generativas |

**OCR:** se mide de forma indirecta, con `fact_accuracy` y `recall` de las preguntas cuyas páginas relevantes son escaneadas (el validador dice cuáles son). No hay medida de calidad del texto carácter a carácter: haría falta una transcripción de referencia, que se puede añadir si se prepara.

## 5. Regla de decisión (fijada antes de ver los resultados)

* `cross_tenant_leaks` e `injection_documents_used` deben ser 0 en todas las configuraciones; si no, es un **bloqueo de producción**.
* Por defecto se queda la configuración más simple, salvo que otra mejore `citation_accuracy` o `fact_accuracy` en al menos 5 puntos sin bajar `abstention_accuracy`:
  * `hybrid` frente a `dense`: si no hay mejora, se queda `dense`;
  * reranker: además, el p95 de latencia debe seguir por debajo de 2 s; si no, apagado.
* Modo generativo: solo si mejora `fact_accuracy` sin aumentar respuestas sin fundamento (`faithfulness` = 1,0, abstención = 1,0) y con un coste por pregunta aceptado por el propietario.

## 6. Resultados

*Pendiente de los documentos reales.* Se añadirán aquí:
* la tabla de las seis configuraciones;
* el desglose por categoría;
* los fallos analizados;
* la decisión de valores por defecto y su justificación.
