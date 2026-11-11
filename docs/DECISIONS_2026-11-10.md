# Decisiones — 2026-11-10

El propietario delegó las decisiones en el asistente ("lo que yo necesito darte una decisión tómala tú"). Igual que `DECISIONS_2026-10-30.md`, cada fila dice qué se decidió, por qué y si quedó **implementado**, solo **decidido** o **no ejecutable** por el asistente (requiere dinero, cuentas, datos o juicio jurídico).

| # | Tema | Decisión | Por qué | Estado |
|---|---|---|---|---|
| 1 | Fusionar PR #61 y los PR de Dependabot #56–#60 | Fusionados con CI en verde (#56 y #57 vía #62, rebasados sobre `main`) | Sin conflictos ni fallos; mantenerlos abiertos solo acumula deuda | **Implementado** |
| 2 | Modelo de embeddings por defecto | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` en lugar de `BAAI/bge-small-en-v1.5` | Documentos en español: recall@1 1,0 frente a 0,8 en el corpus RAG | **Implementado**, con guarda `EMBEDDING_MODEL_MISMATCH`: un índice construido con otro modelo se rechaza (503 en `/ask`, indexado marcado como rechazado) hasta reindexar o fijar `EMBEDDING_MODEL` al modelo anterior |
| 3 | Respuestas a preguntas parafraseadas | Selección semántica de la frase de evidencia, con umbral calibrado **por modelo** (ADR-003) | Con el multilingüe: 28/28 correctas y abstención 1,0. Con `bge-small-en` el mismo umbral respondía a todo (abstención 0,0), así que un modelo sin calibrar usa solo la regla léxica | **Implementado** |
| 4 | Errores de OCR que pasaban sin revisión | Revisión obligatoria de todo valor sostenido por una palabra que Tesseract leyó con confianza < 0,60 (`OCR_WORD_REVIEW_THRESHOLD`) | El caso "Eiemplo" (confianza 0,08 en una página con media 0,9) pasaba sin revisión | **Implementado**; captura de errores 1,0 y carga de revisión 0,331 → 0,310 |
| 5 | RLS (decisión 4 del 30-10) | Fases 1–2 del diseño aprobado: script de roles y políticas sin `FORCE` (migración `0009`), contexto por transacción | Sin efecto en producción mientras la app use el propietario de las tablas; probado en PostgreSQL 17 como rol sin privilegios | **Implementado** fases 1–2. La fase 3 (`FORCE` + `docmind_app`) queda **decidida para después de** resolver las lecturas previas al contexto con `SECURITY DEFINER` y de un periodo en staging, como exige el diseño |
| 6 | Permiso de borrado separado (`documents:delete`) | No se añade: escritura en el proyecto sigue permitiendo borrar | Cambiaría el significado de claves API ya emitidas (contrato público); el borrado es recuperable durante `RETENTION_DELETED_DAYS` | **Decidido** |
| 7 | Concesión automática al creador de un proyecto | Se mantiene sin concesión automática | Owners/admins ya ven todos los proyectos; un `member` que crea un proyecto sin acceso lo nota de inmediato y un admin lo concede: falla cerrado | **Decidido** |
| 8 | Webhooks filtrados por proyecto | No por ahora | Los webhooks solo envían id y evento, sin contenido | **Decidido**; revisar si un cliente lo pide |
| 9 | ContextRAG | Sigue desactivado | Sin despliegue verificado | **Decidido** (sin cambios) |
| 10 | LLM para documentos reales | Vertex AI (Gemini) de pago, en la región más cercana a Chile donde el modelo esté disponible (no verificado: comprobarlo al contratar), sin uso para entrenamiento | Mismo proveedor ya integrado (Gemini), retención y acuerdo de tratamiento de datos, cercanía para Ley 21.719 | **No ejecutable** por el asistente: requiere cuenta y facturación |
| 11 | Documentos reales para evaluar | 30–50 documentos anonimizados de un cliente piloto, con consentimiento | El F1 actual es sobre corpus sintético | **No ejecutable**: requiere datos y consentimiento |
| 12 | Almacenamiento de producción | S3 compatible con SSE-KMS y versionado | Ya soportado en el código; falta validarlo | **No ejecutable**: requiere cuenta |
| 13 | Licencia | Sin cambios (propietaria) | Requiere abogado | **No ejecutable** |

## Riesgos que dejan estas decisiones

* Decisión 2: un despliegue existente que dependía del modelo por defecto verá `EMBEDDING_MODEL_MISMATCH` al actualizar. Es intencionado (fallar cerrado en lugar de mezclar espacios vectoriales). Remedio: fijar `EMBEDDING_MODEL=BAAI/bge-small-en-v1.5` o reindexar.
* Decisión 3: el umbral 0,5 del modelo multilingüe tiene un margen estrecho en el corpus (la peor pregunta que debe abstenerse llega a 0,44; la peor paráfrasis correcta, a 0,52). Se debe re-medir con documentos reales.
* Decisión 5: hasta la fase 3, RLS no protege nada en producción; el aislamiento sigue siendo el de la aplicación (probado ruta a ruta).
