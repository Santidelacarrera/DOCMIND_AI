# Informe Final de Seguridad Empresarial y Modelo de Amenazas

> **Estado:** Auditoría estática y dinámica del repositorio (`bandit`, `pip-audit`, `npm audit`, matriz de 82 rutas, pruebas de aislamiento y RLS en PostgreSQL 17) **COMPLETADA Y VALIDADA**. *Nota: este documento refleja los controles técnicos implementados y probados en el repositorio; no sustituye un pentest externo independiente ni constituye una certificación formal ISO 27001 / SOC 2.*

---

## 1. Modelo de amenazas (STRIDE) y mitigaciones verificadas

| Superficie / Amenaza | Riesgo | Mitigación implementada y archivo de referencia | Evidencia de prueba |
|---|---|---|---|
| **Fuga de datos entre tenants (IDOR / SQL / Vector / Storage)** | Crítico | 1) Comprobación obligatoria `membership()` en cada ruta; 2) Políticas RLS en 26 tablas PostgreSQL (`docmind_app` `NOBYPASSRLS`); 3) Filtro obligatorio `tenant_id` y `project_id` dentro de cada query Qdrant y BM25; 4) Prefijo validado `owned_key(org_id, key)` en almacenamiento | `test_tenant_isolation.py`, `test_rls_policies.py` (55/55), `test_project_authorization.py` |
| **Prompt Injection documental y contenido activo** | Alto | Documentos tratados siempre como datos no confiables: 1) Detección de patrones de inyección en español/inglés (`chunking.py`) que marca fragmentos como `suspicious=True` y los excluye de la recuperación; 2) `safety.py` (`has_active_content`, `strip_invisible`, `safe_attribute`); 3) Verificación de soporte frase por frase en modo generativo (`verification.py`) | `test_safety_and_aliases.py`, `test_generative_answers.py`, `app.evaluation.rag --corpus extended` (`injection_documents_used = 0`) |
| **Exfiltración de PII hacia vectores, logs o LLMs** | Alto | Enmascarado previo (`pii.py`, `rut.py` para RUT chileno con módulo 11, emails, teléfonos, tarjetas) antes de fragmentar, indexar o consultar al LLM; preguntas y textos fuera de los logs (`logging_config.py`) | `test_ingestion.py`, `test_project_authorization.py::test_denied_responses_and_logs_do_not_reveal_names_secrets_or_questions` |
| **Archivos maliciosos, Zip-bombs, Macros OOXML, XSS** | Alto | Validación de magic bytes, tamaño por plan, conteo de páginas, rechazo de PDFs cifrados o con JS/Launch actions (`validation.py`), inspección de entradas ZIP/OOXML contra compresión excesiva, relaciones externas, OLE y macros `vbaProject.bin` (`formats.py`, `docx.py`), escaneo ClamAV fail-closed (`antivirus.py`) | `test_multiformat_ingestion.py`, `test_word_and_citations.py`, `test_antivirus.py` |
| **SSRF en Webhooks salientes** | Alto | Resolución pública previa y en el instante de entrega (`resolve_public`), bloqueo de rangos privados/loopback/link-local/CGNAT/IPv6-mapped, socket fijado a la IP validada (`_pinned_handlers`) y prohibición de redirecciones HTTP 3xx (`_NoRedirect`) en `webhooks.py` | `test_webhooks.py` |
| **Falsificación / Replay de Webhooks de Billing** | Alto | Verificación HMAC-SHA256 con `hmac.compare_digest`, ventana temporal de replay (`BILLING_WEBHOOK_TOLERANCE_SECONDS=300`) e idempotencia por `(provider, event_id)` en `billing_providers.py` y `routers/billing.py` | `test_billing_and_granular_permissions.py` |
| **Inyección de fórmulas en exportaciones CSV/XLSX** | Medio | `neutralize_formula()` antepone comilla simple a valores que comiencen por `=`, `+`, `-`, `@`, tabulador o retorno de carro | `test_api.py` |
| **Secuestro de sesión / Fuerza bruta / Abuso de API** | Alto | Hash de contraseñas con Argon2 (`pwdlib.PasswordHash.recommended()`), verificación en tiempo constante ante usuarios inexistentes (`_DUMMY_HASH`), `session_version` (`sv`) en JWT para invalidar sesiones al cambiar contraseña o revocar miembro, cookies `HttpOnly`/`SameSite`/`Secure`, rate limiting en Redis por ruta | `test_api.py`, `test_readiness_security.py` |

---

## 2. Resultados de escaneos de seguridad ejecutados (2026-11-16)

1. **Análisis SAST Python (`bandit -r app -ll -q`)**: **0 hallazgos** de severidad media o alta (`exit 0`).
2. **Auditoría de dependencias Python (`pip-audit`)**: **0 vulnerabilidades conocidas** en el entorno de dependencias (`exit 0`).
3. **Auditoría de dependencias Node.js (`npm audit`)**: **0 vulnerabilidades** (`found 0 vulnerabilities` con `next@16.4.5` y `react@19.3.1`).
4. **Matriz de autorización (`scripts/authorization_matrix.py --check`)**: **82 rutas** auditadas; ninguna ruta de negocio carece de control de autorización.
