# Índice de documentación

Qué está vigente y qué es histórico. Revisado el 2026-11-12. Si dos documentos se contradicen, manda el de la sección «Estado actual».

## Estado actual

| Documento | Para qué |
|---|---|
| [DOCMIND_IMPLEMENTATION_STATUS.md](DOCMIND_IMPLEMENTATION_STATUS.md) | Qué está implementado y validado |
| [DOCMIND_TECHNICAL_DEBT.md](DOCMIND_TECHNICAL_DEBT.md) | Deuda abierta y saldada, con evidencia |
| [DOCMIND_ROADMAP.md](DOCMIND_ROADMAP.md) | Siguiente trabajo por prioridad y lo bloqueado por el propietario |
| [DOCMIND_ARCHITECTURE_AUDIT.md](DOCMIND_ARCHITECTURE_AUDIT.md) | Auditoría de arquitectura de referencia |
| [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md) | Lo que falta para salir a producción, separando lo hecho de lo que requiere al propietario |
| [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) | Límites conocidos del producto |
| [UNIFICATION_PROGRESS.md](UNIFICATION_PROGRESS.md) | Unificación de ContextRAG dentro de DOCMIND (ADR-005): auditoría, matriz, entregas y pendientes |
| [decisions/](decisions/) y [DECISIONS_2026-11-10.md](DECISIONS_2026-11-10.md) | ADR y decisiones vigentes |

## Uso y operación

[INSTALLATION.md](INSTALLATION.md) · [ENVIRONMENT.md](ENVIRONMENT.md) · [DEVELOPMENT.md](DEVELOPMENT.md) · [TESTING.md](TESTING.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) · [DEPLOYMENT.md](DEPLOYMENT.md) · [STAGING.md](STAGING.md) · [OPERATIONS.md](OPERATIONS.md) · [OBSERVABILITY.md](OBSERVABILITY.md) (alertas en `deploy/prometheus/`) · [RELIABILITY.md](RELIABILITY.md) · [API.md](API.md)

## Diseño y seguridad

[architecture.md](architecture.md) · [RLS_DESIGN.md](RLS_DESIGN.md) · [rls/](rls/) · [SECURITY.md](SECURITY.md) · [security/threat-model.md](security/threat-model.md) · [AUTHORIZATION_MATRIX.md](AUTHORIZATION_MATRIX.md) · [AUTHORIZATION_POLICY.md](AUTHORIZATION_POLICY.md) · [DOCUMENT_AUTHORIZATION.md](DOCUMENT_AUTHORIZATION.md) · [DATA_RETENTION.md](DATA_RETENTION.md) · [INGESTION.md](INGESTION.md) · [LOCAL_PROCESSING.md](LOCAL_PROCESSING.md) · [EXTERNAL_DEPENDENCIES.md](EXTERNAL_DEPENDENCIES.md) · [CASE_QUERY.md](CASE_QUERY.md) · [CONTEXTRAG_INTEGRATION.md](CONTEXTRAG_INTEGRATION.md)

## Calidad

[EVALUATION.md](EVALUATION.md) y los informes en [evaluation/](evaluation/) (`baseline-report.*`, `rag-report*.*`, `hybrid-retrieval-2026-11-12.md`).

## Demo, piloto y negocio

[DEMO_EMPRESARIAL.md](DEMO_EMPRESARIAL.md) (guion vigente) · [DEMO.md](DEMO.md) · [PILOT.md](PILOT.md) · [LICENSING.md](LICENSING.md) · [NOTA_PROYECTO.md](NOTA_PROYECTO.md) · [empresa.md](empresa.md)

## Histórico (no reflejan el estado actual)

| Documento | Superado por |
|---|---|
| [AUDIT_2026-10-26.md](AUDIT_2026-10-26.md), [AUDIT_PHASE2_2026-10-26.md](AUDIT_PHASE2_2026-10-26.md), [AUDIT_FINAL_2026-10-26.md](AUDIT_FINAL_2026-10-26.md), [AUDIT_PRODUCTION_2026-10-27.md](AUDIT_PRODUCTION_2026-10-27.md) | `DOCMIND_*.md` (2026-11-10 en adelante) |
| [DECISIONS_2026-10-30.md](DECISIONS_2026-10-30.md) | `DECISIONS_2026-11-10.md` y los ADR, salvo lo que estos no traten |
