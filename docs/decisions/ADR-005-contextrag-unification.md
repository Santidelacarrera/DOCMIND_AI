# ADR-005 — Unificar ContextRAG dentro de DOCMIND como módulo `app/rag`

**Estado:** aceptado el 2026-11-12. Primera entrega implementada: recuperación híbrida, detección de conflictos entre fuentes y patrones de inyección. El resto del plan está en `docs/UNIFICATION_PROGRESS.md`.

## Contexto

Hasta ahora DOCMIND y ContextRAG_Engine eran dos productos. DOCMIND enviaba texto enmascarado a un ContextRAG desplegado aparte, mediante un outbox (`contextrag_sync.py`, desactivado por defecto). ContextRAG nunca se desplegó en un entorno accesible. Esa arquitectura implicaba:

* dos backends, dos modelos de documento (páginas de DOCMIND frente a parent/child de ContextRAG) y dos chunkers;
* dos sistemas de autorización: miembros y proyectos en DOCMIND, claves por tenant y grupos ACL en ContextRAG. Revocar un miembro en DOCMIND no cambiaba nada en ContextRAG;
* respuestas que citan texto enmascarado reconstruido en otro servicio.

La auditoría (`docs/UNIFICATION_PROGRESS.md`, matriz) muestra que lo valioso de ContextRAG son algoritmos puros, sin dependencias externas: BM25, fusión RRF, verificación por implicación, conflictos, presupuesto de contexto, inyección y memoria. No es su infraestructura.

## Decisión

1. **Monolito modular.** Las capacidades de ContextRAG se incorporan como el paquete `backend/app/rag/`. No se crea un servicio nuevo ni un segundo backend. ContextRAG no se instala como dependencia: el código se porta, se adapta y se prueba aquí, con su origen indicado en cada módulo.
2. **Un solo modelo documental.** Todo opera sobre los fragmentos canónicos de DOCMIND: los puntos de la colección Qdrant única, con `tenant_id`, `project_id`, `document_id`, `page` y `chunk_index`, y el texto enmascarado. No hay un segundo chunker. Si más adelante se adopta parent/child, los padres serán agrupaciones de estos mismos fragmentos.
3. **La autorización es la de DOCMIND.** Toda lectura pasa por el filtro de tenant en la consulta Qdrant y por las comprobaciones de proyecto y vigencia de `answering._retrieve`. Las claves por tenant de ContextRAG no se portan.
4. **Por configuración y medido.** Cada capacidad que cambia resultados se activa por configuración y se mide con `python -m app.evaluation.rag` antes de cambiar su valor por defecto. `RETRIEVAL_MODE` queda en `dense`; la medición está en `docs/evaluation/hybrid-retrieval-2026-11-12.md`.
5. **El outbox hacia ContextRAG externo se mantiene, desactivado.** No se borra mientras pueda haber un despliegue que lo use. Queda marcado para retirar cuando las capacidades restantes estén en `app/rag`.

## Primera entrega

| Módulo | Origen en ContextRAG | Uso en DOCMIND |
|---|---|---|
| `app/rag/bm25.py` | `retrieval/bm25.py` (`IncrementalBM25Index`) | BM25Plus sin `rank_bm25` |
| `app/rag/fusion.py` | `retrieval/hybrid_search.py` | RRF de los rankings vectorial y léxico |
| `app/rag/lexical.py` | `BM25Index` con *loader* | Corpus léxico por organización, cargado con `scroll_chunks` (filtro de tenant, sin fragmentos sospechosos), caché con TTL e invalidación local |
| `app/rag/conflicts.py` | `verification/conflicts.py` (`StructuredConflictDetector`) | Campo `conflicts` de `/ask`: posibles discrepancias de fechas, cifras o negación con la fuente citada |
| `app/ingestion/text.py` | `verification/injection.py` | Patrones en español y de ocultación. Se excluyen los que dan falsos positivos en documentos de empresa |

## Consecuencias

* `/ask` añade el campo `conflicts`: un cambio aditivo y compatible. La interfaz muestra «Sources that may disagree».
* En modo `hybrid`, cada proceso mantiene en memoria el texto enmascarado de los fragmentos de las organizaciones consultadas, como máximo `RETRIEVAL_LEXICAL_MAX_CHUNKS` por organización. Un corpus de otro proceso puede tener hasta `RETRIEVAL_LEXICAL_CACHE_SECONDS` de antigüedad. Un documento borrado no se cita nunca: lo impiden `is_live` y la ausencia de su vector. Un documento nuevo tarda como máximo el TTL en aparecer por vía léxica.
* El detector de conflictos es heurístico y sus umbrales no están calibrados para DOCMIND. Avisa, nunca decide.
