# Limitaciones conocidas (2026-11-10)

Lo que DocMind AI **no** garantiza hoy. Cada punto tiene su evidencia o su documento.

## Calidad

* **Métricas sobre datos sintéticos.** El F1 de extracción (93 %) y las 28/28 respuestas RAG se miden sobre documentos ficticios escritos por el equipo. No hay evaluación con documentos reales de clientes ([EVALUATION.md](EVALUATION.md)).
* **LLM real evaluado solo con Gemini gratuito** sobre el corpus sintético (`evaluation/gemini-report.md`); la capa gratuita no es apta para datos confidenciales ([DECISIONS_2026-11-10.md](DECISIONS_2026-11-10.md), decisión 10).
* **Respuestas extractivas.** `/ask` cita frases literales de los documentos; no resume ni combina información de varios fragmentos.
* **Umbral semántico calibrado en un solo modelo** y con margen estrecho (0,44 frente a 0,52) en el corpus (ADR-003).
* **Fechas numéricas ambiguas** (03/04/2025) se marcan para revisión; no se resuelven solas.

## Seguridad y datos

* **RLS solo preparada (fases 1–2).** Las políticas existen y están probadas, pero la aplicación conecta como propietario de las tablas: el aislamiento efectivo es el de la aplicación, probado ruta a ruta ([RLS_DESIGN.md](RLS_DESIGN.md)).
* **El enmascarado de PII es por reglas** (correo, IBAN, tarjeta, DNI/NIE, SSN, RUT, teléfono, IP; nombres por contexto). Nombres sin contexto pueden no detectarse sin el detector spaCy.
* **`/metrics` no está autenticado**: restringirlo en el ingress.
* **Sin permiso de borrado separado**: quien escribe en un proyecto puede borrar sus documentos (recuperables durante la retención).

## Operación

* **No desplegado en staging ni producción.** S3/KMS, ClamAV y SMTP reales no verificados; copia y restauración verificadas solo en local con almacenamiento local.
* **Qdrant sin copia propia**: los vectores se reconstruyen reprocesando.
* **Un solo planificador (beat)**: debe haber exactamente una instancia.
* **Word sin vista previa**: se descarga el original; las páginas de Word sin saltos explícitos son estimadas (`~p.N`).

## Producto

* Un revisor por ejecución de extracción; sin asignación de revisores.
* Webhooks sin filtro por proyecto (solo envían id y evento).
* Licencia propietaria pendiente de revisión jurídica.
