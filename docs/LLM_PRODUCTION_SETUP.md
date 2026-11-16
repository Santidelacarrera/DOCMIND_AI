# Configuración y Evaluación de Proveedores LLM Reales (Vertex AI, OpenAI y Gemini)

> **Estado:** Adaptadores para `vertex`, `openai` y `gemini`, modo generativo verificado frase por frase (`ASK_MODE=generative`), control de costes, métricas Prometheus y script de verificación [`backend/scripts/vertex_check.py`](../backend/scripts/vertex_check.py) **COMPLETADOS Y PROBADOS CON DOBLES DE PRUEBA Y GENERADOR OFFLINE**. La validación con llamadas de pago reales está **BLOQUEADA POR CREDENCIALES** del propietario.

---

## 1. Proveedores implementados y controles de seguridad

| Proveedor (`LLM_PROVIDER`) | Extracción estructurada (`processing.py`) | Respuestas generativas verificadas (`rag/generation.py`) | Controles activos |
|---|---|---|---|
| `vertex` | `VertexGeminiLLMProvider` (ADC / `google-auth`, sin API key estática si corre con Workload Identity) | `ChatGenerator("vertex")` | Timeout `OPENAI_TIMEOUT_SECONDS`, reintentos con backoff, estimación de tokens/coste (`GEMINI_INPUT_PRICE_PER_1M` / `GEMINI_OUTPUT_PRICE_PER_1M`), alerta Prometheus `DocMindLlmSpendHigh` |
| `openai` | `OpenAILLMProvider` | `ChatGenerator("openai")` | Timeout, `OPENAI_MAX_RETRIES`, precios por millón (`OPENAI_INPUT_PRICE_PER_1M` / `OPENAI_OUTPUT_PRICE_PER_1M`), validación estricta de esquema JSON |
| `gemini` | `GeminiLLMProvider` | `ChatGenerator("gemini")` | Uso con `GEMINI_API_KEY` para pruebas controladas con datos sintéticos |
| `mock` | `MockLLMProvider` (determinista por reglas/regex) | `OfflineExtractiveGenerator` (en evaluación offline) | Coste $0, 0 llamadas de red |

### Protección de datos e invariantes en `ASK_MODE=generative`
1. **Enmascarado previo de PII**: tanto los fragmentos almacenados en Qdrant como la pregunta enviada al modelo pasan por `pipeline.mask_query` (RUT chileno, correos, teléfonos, tarjetas).
2. **Guardas contra contenido activo**: [`backend/app/rag/safety.py`](../backend/app/rag/safety.py) elimina caracteres de control invisibles (`strip_invisible`), sanea nombres de fuente (`safe_attribute`) y descarta pasajes o frases con contenido activo (`has_active_content`).
3. **Verificación frase por frase (`verify`)**: cada oración generada por el LLM debe citar un pasaje `[n]` y superar el umbral de soporte léxico/entidades (`ASK_GENERATION_THRESHOLD=0.95`) más resolución de alias ([`backend/app/rag/aliases.py`](../backend/app/rag/aliases.py)). Toda oración no respaldada se elimina (`dropped_claims`); si no queda ninguna oración verificada, el sistema se abstiene explícitamente.
4. **Control de cuotas por organización**: antes de ejecutar `/api/v1/ask`, [`plans.check_query_quota`](../backend/app/plans.py) comprueba tanto el límite mensual de consultas (`max_queries_per_month`) como el presupuesto mensual de tokens de IA (`max_ai_tokens_per_month`), respondiendo `402 QUOTA_EXCEEDED` si se supera.

---

## 2. Ejecución de `scripts/vertex_check.py`

### En seco (sin coste, ejecutado en esta auditoría)
```bash
cd backend
python scripts/vertex_check.py
```

### Con credenciales reales de Vertex AI (cuando el propietario las habilite)
```bash
export LLM_PROVIDER=vertex
export VERTEX_PROJECT=<id-proyecto-gcp>
export VERTEX_LOCATION=southamerica-east1
export GEMINI_INPUT_PRICE_PER_1M=0.15
export GEMINI_OUTPUT_PRICE_PER_1M=0.60

cd backend
python scripts/vertex_check.py --yes
```
El comando realiza exactamente dos llamadas pequeñas sobre textos ficticios (una de generación verificada con citas y una de extracción JSON sobre factura sintética), informando latencia en ms, tokens de prompt/completion, coste estimado en USD y número de afirmaciones verificadas.
