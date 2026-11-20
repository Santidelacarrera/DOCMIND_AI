# Protocolo de Respuesta y Notificación de Brechas de Seguridad — Ley N° 21.719 (Chile)

Este protocolo define el procedimiento operativo y técnico ante incidentes que comprometan la confidencialidad, integridad o disponibilidad de datos personales tratados en **DocMind AI** (Art. 14 quinquies, Ley N° 21.719).

---

## 1. Clasificación de Severidad

| Nivel | Criterio | Ejemplo | Acción inmediata |
|---|---|---|---|
| **SEV-1 (Crítica)** | Acceso no autorizado confirmado o exfiltración de documentos, PII o credenciales de clientes | Fuga de clave de BD/S3, compromiso de cuenta `owner`, fuga cross-tenant | Contención inmediata (< 1 h) + notificación a clientes afectados (< 24 h) y preparación de reporte a la Agencia |
| **SEV-2 (Alta)** | Vulnerabilidad explotable sin evidencia aún de exfiltración, o exposición temporal acotada | Token API comprometido, error de configuración CORS/RLS | Revocación/parche inmediato + análisis forense en `audit_logs` |
| **SEV-3 (Media/Baja)** | Intento bloqueado o incidente sin impacto en datos personales | Brute force bloqueado por rate-limit, malware rechazado por ClamAV | Registro interno y ajuste de reglas |

---

## 2. Fases de Respuesta Técnica en DocMind AI

### Fase 1 — Detección y Contención (0 – 2 horas)
1. **Identificar alcance:** Determinar `organization_id`(s) y rango horario afectado usando `audit_logs` y logs de Railway (`api`, `worker`).
2. **Revocar accesos comprometidos:**
   - Revocar API keys comprometidas vía `DELETE /api/v1/api-keys/{id}` o directamente en la tabla `api_keys` (`revoked_at = NOW()`).
   - Invalidar sesiones JWT activas rotando `JWT_SECRET` en Railway (fuerza re-autenticación global inmediata).
   - Si hubo sospecha de exposición de `CONVERSATION_ENCRYPTION_KEY`, rotar a una nueva clave y mover la anterior a `CONVERSATION_PREVIOUS_ENCRYPTION_KEY`, luego re-cifrar o purgar según política.
   - Si hubo compromiso de credenciales de subencargados (GCP Vertex AI, S3, Stripe), rotar inmediatamente en el panel del proveedor y actualizar variables en Railway.
3. **Preservar evidencia:** Exportar snapshot de `audit_logs` y logs del periodo del incidente en almacenamiento de solo lectura antes de cualquier limpieza.

### Fase 2 — Evaluación de Impacto en Datos Personales (2 – 12 horas)
Documentar:
- Tipo de incidente (confidencialidad, integridad o disponibilidad).
- Categorías y volumen aproximado de titulares y documentos afectados (distinguiendo si incluían datos sensibles o datos de menores).
- Si los datos estaban cifrados (AES-GCM en conversaciones, Argon2id en contraseñas, PII enmascarada en Qdrant) — lo cual reduce materialmente el riesgo para los titulares.
- Medidas de mitigación ya ejecutadas.

### Fase 3 — Notificación Obligatoria (Ley N° 21.719)
1. **A clientes B2B (Responsables de Datos):** Notificar sin dilación indebida (objetivo interno: **máximo 24 horas** desde la confirmación) al contacto de seguridad/privacidad de cada organización afectada, entregando resumen técnico, alcance y acciones recomendadas.
2. **A la Agencia de Protección de Datos Personales (cuando DocMind AI actúa como Responsable, o apoyando al cliente B2B):** Reportar sin dilación indebida conforme a las instrucciones oficiales de la Agencia cuando la brecha implique un riesgo razonable para los derechos y libertades de los titulares.
3. **A los titulares afectados:** Cuando exista alto riesgo (ej. exposición en claro de datos sensibles, financieros o credenciales), comunicar en lenguaje claro qué ocurrió, qué datos se vieron afectados, medidas adoptadas y pasos recomendados (ej. cambio de contraseña).

### Fase 4 — Cierre y Post-Mortem (3 – 7 días)
1. Registrar el incidente en el **Registro Interno de Brechas de Seguridad** (obligatorio incluso si no requirió notificación externa).
2. Implementar test de regresión automatizado (`backend/tests/` o `frontend/tests/e2e/`) que prevenga la recurrencia.
3. Actualizar este protocolo y el `threat-model.md` con las lecciones aprendidas.

---

## 3. Consulta Forense Rápida (`audit_logs`)

```sql
SELECT created_at, organization_id, actor_id, action, target_type, target_id
FROM audit_logs
WHERE created_at >= NOW() - INTERVAL '48 hours'
ORDER BY created_at DESC
LIMIT 200;
```
