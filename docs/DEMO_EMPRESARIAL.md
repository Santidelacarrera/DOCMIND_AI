# Guion de demostración empresarial (≈ 20 minutos)

Para mostrar DocMind AI a un cliente o a ingenieros. Todo con documentos ficticios. La demo técnica sin interfaz está en [DEMO.md](DEMO.md).

## Preparación (antes de la reunión)

```bash
cp .env.example .env
docker compose up --build --wait
python backend/scripts/staging_smoke.py --base-url http://localhost:8000 --ask   # debe decir "smoke test passed"
cd backend && python -m app.ingestion.demo --output-dir ../demo_output          # genera los documentos ficticios
```

**Extracción real (paso 5):** con `LLM_PROVIDER=mock` (el valor por defecto) la extracción devuelve valores fijos de prueba, útiles para mostrar el flujo pero no el contenido. Para extraer de verdad, pon en `.env` `LLM_PROVIDER=gemini` y `GEMINI_API_KEY` (solo documentos ficticios con la capa gratuita) y crea el esquema chileno en **Schemas** con el JSON `DTE_CL_SCHEMA` de `backend/app/evaluation/corpus.py`.

Ten a mano: `demo_output/documentos_ficticios/` (contrato, política de vacaciones, manual de seguridad), `backend/eval_corpus/pdfs/cl_factura_01.pdf` (factura chilena) y `cl_factura_scan_05.pdf` (escaneada). Dos navegadores (o una ventana privada) para las dos organizaciones.

## 1. Inicio de sesión (1 min)
<http://localhost:3000> → **Register**: `demo-aurora@example.com`, organización *Aurora Demo*. Mensaje: contraseñas con Argon2id, sesión con cookie `HttpOnly` y CSRF; el servidor es la única frontera de autorización.

## 2. Organización y proyecto (1 min)
En **Documents**, crear el proyecto *Contratos 2026*. En **Members**, mostrar roles (owner, admin, member, viewer) y que los permisos se conceden por proyecto.

## 3. Carga de documentos (2 min)
Subir el contrato (PDF), la política de vacaciones (Word) y la factura chilena. Mensaje: se valida tipo real (magic bytes), tamaño y páginas; Word con macros se rechaza; los duplicados se detectan por SHA-256.

## 4. Procesamiento (2 min)
La lista muestra el estado en vivo (en cola → procesando → completado). Subir la factura **escaneada**: pasa por OCR. Mensaje: si falla, se reintenta de forma idempotente y nunca queda colgado (beat recupera trabajos atascados).

## 5. Extracción y revisión (4 min)
Abrir la factura chilena (con el LLM real y el esquema chileno): tipo, folio, fecha, emisor, receptor, RUT y monto, cada uno con su evidencia en el documento y su confianza; un RUT con dígito verificador incorrecto se marca para revisión (`format: cl-rut`). En la escaneada, todo valor apoyado en una palabra que el OCR leyó con duda se marca para revisión. Con el proveedor `mock`, mostrar en su lugar la marca "no encontrado en el documento" sobre los valores fijos: el sistema avisa cuando un valor no está en el texto. Corregir el valor con un motivo y aprobar. Exportar a XLSX (protegido contra inyección de fórmulas).

## 6. Pregunta en lenguaje natural (3 min)
En **Ask**: «¿Cuál es el precio total del contrato?». La respuesta cita el texto literal.

## 7. Respuesta con cita (1 min)
Mostrar bajo la respuesta: documento, página, número de fragmento y el extracto exacto. Mensaje: el sistema no genera texto; cita.

## 8. Verificación de la fuente y abstención (2 min)
Pulsar la cita: abre el documento original. Preguntar «¿Cuál es el color favorito del gerente?»: responde que no encontró información, sin inventar. Mostrar `docs/evaluation/rag-report.md`: abstención correcta 100 %, 0 fugas entre organizaciones.

## 9. Aislamiento entre organizaciones (3 min)
En la ventana privada, registrar *Orion Demo* y preguntar lo mismo: no ve nada de Aurora. Copiar la URL de un documento de Aurora en la sesión de Orion: 404, igual que un documento inexistente. Mensaje: probado ruta a ruta en CI, en el índice vectorial (filtro dentro de la consulta) y, en preparación, con Row-Level Security en PostgreSQL (`tests/test_rls_policies.py`).

## Preguntas difíciles y respuestas honestas

* *¿Qué precisión tiene con mis documentos?* No medida aún: las cifras son sobre un corpus sintético. Propuesta: piloto con 30–50 documentos anonimizados ([KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)).
* *¿Dónde van mis datos?* Por defecto, nada sale de la máquina: OCR, enmascarado, embeddings y respuestas son locales. Con un LLM externo, solo el texto del documento hacia el proveedor elegido.
* *¿Está en producción?* No: apto para piloto controlado ([DECISIONS_2026-11-10.md](DECISIONS_2026-11-10.md)).
