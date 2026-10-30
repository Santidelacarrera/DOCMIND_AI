# Decisiones de producto — 2026-10-30

Decididas por el propietario del proyecto, que delegó la elección en el asistente ("toma tú las decisiones"). Responden a `docs/AUDIT_PRODUCTION_2026-10-27.md`, sección 8. Cada decisión dice si **se implementó** o solo **se decidió**.

| # | Tema | Decisión | Estado |
|---|---|---|---|
| 1 | Totales de uso (`GET /usage`) | Opción A: solo administradores. Las claves API siguen por scope `usage:read` | **Implementada** y probada (`tests/test_usage_visibility.py`) |
| 2 | Pantalla de preguntas | Sí: página `/ask` sobre `POST /api/v1/ask`, con citas (documento y página) y abstención explícita | **Implementada** (`frontend/app/ask`); se activa solo si el servidor tiene índice vectorial |
| 3 | Vista previa de Word | No se convierte a PDF (exigiría LibreOffice, más superficie de ataque). El visor avisa y ofrece descargar el original; cada valor ya cita su página | **Implementada** (aviso en el visor) |
| 4 | RLS | Se aprueba el diseño de `docs/RLS_DESIGN.md` con funciones `SECURITY DEFINER` para las consultas previas al contexto (no un rol `docmind_system`). Fases: roles → políticas sin `FORCE` → `FORCE` en staging | **Solo decidida**; sin migración todavía. El aislamiento actual lo garantiza la aplicación y sus pruebas negativas |
| 5 | Texto enmascarado hacia ContextRAG | Solo `masked`. `raw` sigue bloqueado en staging y producción y no se habilita | **Decidida**; ya es el comportamiento actual |
| 6 | Sincronización con ContextRAG | Desactivada en todos los entornos salvo pruebas, hasta desplegar ContextRAG y verificar el outbox | **Decidida**; es el valor por defecto |
| 7 | Borrado de huérfanos (`STORAGE_RECONCILE_DELETE`) | Se mantiene en `false` hasta revisar un informe real | **Decidida**; es el valor por defecto |
| 8 | Titularidad y licencia | Sin cambios: no se puede decidir en nombre del titular | Pendiente de la persona titular |

## Lo que sigue abierto

* **Evaluación con LLM real**: la clave `OPENAI_API_KEY` del `.env` local está vacía; falta una clave. Comando: `python -m app.evaluation --provider openai`.
* **RLS**: implementar la migración por fases (decisión 4).
* **Veredicto**: sigue siendo "apto para piloto controlado, no para producción" hasta cerrar los dos puntos anteriores.
