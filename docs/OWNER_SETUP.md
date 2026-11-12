# Configuración que solo puede hacer el propietario

Tres cosas desbloquean lo que falta validar de la unificación (`docs/UNIFICATION_PROGRESS.md`). Ninguna credencial ni secreto se guarda en el repositorio ni en Obsidian. Los valores de ejemplo son marcadores (`<...>`), no credenciales.

## 1. Vertex AI (respuestas generativas y extracción con Gemini)

**Qué desbloquea:** validar `ASK_MODE=generative` con un modelo real. Hoy solo está probado con un modelo simulado y con el generador sin conexión.

1. **Proyecto de Google Cloud** con facturación activa. Activar la API: `gcloud services enable aiplatform.googleapis.com --project <PROYECTO>`.
2. **Región:** elegir según la residencia de los datos del cliente (por ejemplo `southamerica-east1`, São Paulo, o `us-central1`). Debe coincidir con el contrato o la política de datos.
3. **Identidad sin claves en ficheros** (preferido):
   * GKE / Cloud Run: Workload Identity, con una cuenta de servicio dedicada (`docmind-llm@<PROYECTO>.iam.gserviceaccount.com`) y solo el rol `roles/aiplatform.user`.
   * En una máquina propia: `gcloud auth application-default login` (solo para el ensayo).
   * Si hace falta un fichero JSON de cuenta de servicio: guardarlo **fuera del repositorio**, con permisos de solo lectura para el usuario del servicio, y apuntar `GOOGLE_APPLICATION_CREDENTIALS` a esa ruta. Nunca en `.env` versionado.
4. **Variables** (en el gestor de secretos o en el entorno del despliegue):
   ```
   LLM_PROVIDER=vertex
   VERTEX_PROJECT=<PROYECTO>
   VERTEX_LOCATION=<REGIÓN>
   VERTEX_MODEL=            # vacío = GEMINI_MODEL
   GEMINI_INPUT_PRICE_PER_1M=<precio vigente>
   GEMINI_OUTPUT_PRICE_PER_1M=<precio vigente>
   ASK_MODE=generative      # solo cuando se quiera activar en ese entorno
   ```
5. **Comprobación** (instalar antes el extra `vertex`: `pip install ".[vertex]"`):
   ```bash
   cd backend
   python -m app.evaluation.rag --corpus extended --retrieval hybrid --answer-mode generative
   ```
   Este comando usa el generador sin conexión: sirve de referencia. Para medir el modelo real hay que lanzar la API con las variables anteriores y hacer preguntas, o ejecutar la evaluación con documentos reales (apartado 3). Revisar `dropped_claims` y la tasa de abstención frente al modo extractivo.
6. **Coste:** cada pregunta en modo generativo es una llamada al modelo. La métrica `docmind_llm_estimated_cost_total{provider="vertex"}` y la alerta `DocMindLlmSpendHigh` (más de 20 en 24 h) vigilan el gasto. Conviene configurar además un presupuesto con alerta en Google Cloud Billing.

## 2. Clave de cifrado de conversaciones

**Qué desbloquea:** las conversaciones (`/conversations`). Sin la clave, la API responde 503 y no guarda nada.

1. **Generarla en una máquina de confianza** (sale por pantalla una vez; no pegarla en chats, tickets ni notas):
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
2. **Guardarla en el gestor de secretos** del entorno (Google Secret Manager, el de la plataforma de despliegue o un `.env` de producción fuera del repositorio con permisos 600) como `CONVERSATION_ENCRYPTION_KEY`. Una clave distinta por entorno (staging y producción).
3. **Copia de seguridad de la clave**, separada de las copias de la base de datos: sin ella, las copias de `conversation_messages` son ilegibles, y esa es la intención.
4. **No hay rotación automática.** Cambiar la clave deja ilegibles las conversaciones anteriores (la API responde 500 `CONVERSATION_INTEGRITY` al abrirlas). Para rotarla hay que borrar antes las conversaciones o aceptar perderlas. La rotación con doble clave queda como trabajo futuro (`DOCMIND_TECHNICAL_DEBT.md`).
5. **Retención:** `CONVERSATION_RETENTION_DAYS` (90 por defecto) debe coincidir con la política de datos acordada con el cliente.

## 3. Evaluación con documentos reales

**Qué desbloquea:** decidir los valores por defecto (`RETRIEVAL_MODE`, `RERANK_MODEL`, `ASK_MODE`) con datos reales y no solo con los corpus ficticios.

1. **Documentos:** de 30 a 50, con permiso del cliente para usarlos en pruebas y **anonimizados antes de salir del cliente** (nombres, RUT, cuentas, direcciones). Variedad: contratos, pólizas, facturas, actas, documentos escaneados, tablas y documentos largos. Incluir casos difíciles: dos versiones que se contradicen, identificadores (números de expediente o de factura) y preguntas sin respuesta.
2. **Ubicación:** `backend/eval_private/`, ignorada por git (`.gitignore`). Nunca en el repositorio, en Obsidian ni en un servicio externo sin autorización.
3. **Preguntas:** un fichero `backend/eval_private/corpus.json` con el mismo formato que `backend/eval_corpus/rag_questions_extended.json`, pero con `path` en vez de `pages`:
   ```json
   {
     "tenants": {"cliente": "Cliente piloto (anonimizado)", "otro": "Organización de control"},
     "documents": [{"tenant": "cliente", "file": "poliza_hogar.pdf", "path": "docs/poliza_hogar.pdf"}],
     "questions": [
       {"tenant": "cliente", "category": "factual", "question": "¿Qué cubre la póliza por daños por agua?",
        "relevant": [["poliza_hogar.pdf", 3]], "contains": "3.000 euros"},
       {"tenant": "otro", "category": "cross_tenant", "question": "¿Qué cubre la póliza por daños por agua?",
        "relevant": [], "contains": ""}
     ]
   }
   ```
   * `relevant`: documento y página donde está la respuesta.
   * `contains`: el dato literal que debe aparecer en la respuesta.
   * Categorías: `factual`, `paraphrase`, `identifier`, `table`, `long_document`, `contradiction` (con `"expect_conflict": true`), `injection` (con `"forbidden": ["fichero.pdf"]`), `unanswerable`, `cross_tenant`.
   * Las preguntas las debe escribir alguien que conozca los documentos, **antes** de ver las respuestas del sistema.
4. **Ejecutar** las combinaciones y guardar los informes fuera del repositorio:
   ```bash
   cd backend
   for r in dense hybrid; do
     python -m app.evaluation.rag --embedder local --corpus eval_private/corpus.json --retrieval $r \
       --output-dir eval_private/reports/$r
   done
   python -m app.evaluation.rag --embedder local --corpus eval_private/corpus.json \
     --rerank-model BAAI/bge-reranker-v2-m3-int8 --output-dir eval_private/reports/rerank
   ```
   La primera ejecución con `--embedder local` o `--rerank-model` descarga los modelos si `EMBEDDING_LOCAL_FILES_ONLY=false`. En producción se dejan en `EMBEDDING_CACHE_DIR` y se lee sin red.
5. **Decidir** con los informes: activar `hybrid` o el reranker solo si mejoran `recall@1` o `citation_accuracy` **sin** empeorar `abstention_accuracy` ni aumentar fugas, y si la latencia añadida es aceptable. `cross_tenant_leaks` e `injection_documents_used` deben ser 0 siempre. Registrar la decisión en `docs/UNIFICATION_PROGRESS.md` y en la nota del proyecto.

## 4. Lo que requiere autorización explícita (no se hará sin ella)

* **Retirar la sincronización con ContextRAG externo** (`contextrag_sync.py`, tareas de beat, tabla `contextrag_events`, ruta de administración). Implica una migración que borra una tabla. Hasta que se autorice, todo sigue como está: desactivado por defecto y con sus pruebas pasando.
* **Activar RLS en staging/producción** (pasos en `docs/RLS_DESIGN.md`, «Activación»).
* **Cualquier despliegue público.**
