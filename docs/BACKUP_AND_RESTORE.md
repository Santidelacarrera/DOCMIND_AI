# Procedimiento de Backups, Verificación, Restauración y Monitoreo

> **Estado:** Script automatizado [`deploy/backup/pg_backup.sh`](../deploy/backup/pg_backup.sh), servicio `backup` en [`docker-compose.prod.yml`](../docker-compose.prod.yml), reglas de alerta Prometheus [`deploy/prometheus/alerts.yml`](../deploy/prometheus/alerts.yml) y plantilla parametrizada [`deploy/prometheus/alertmanager.yml`](../deploy/prometheus/alertmanager.yml) **COMPLETADOS Y PROBADOS**.

---

## 1. Backups automáticos y verificación de integridad (`deploy/backup/pg_backup.sh`)

El contenedor `backup` (perfil `backup` en `docker-compose.prod.yml`) ejecuta periódicamente:

1. Volcado transaccional en formato personalizado comprimido: `pg_dump -Fc --no-owner -f <archivo>.partial`.
2. **Verificación inmediata de lectura del catálogo**: `pg_restore --list <archivo>.partial >/dev/null`. Si falla, borra el archivo parcial y termina con código `!= 0` para disparar alerta y reintento.
3. **Firma SHA-256**: genera `<archivo>.dump.sha256` junto al volcado verificado.
4. **Retención configurable**: elimina volcados y sumas SHA-256 con antigüedad mayor a `BACKUP_RETENTION_DAYS` (14 días por defecto).

### Ejecución manual de un backup único
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile backup run --rm backup --once
```

---

## 2. Procedimiento de restauración en entorno aislado

```bash
# 1. Comprobar integridad SHA-256 y catálogo del archivo de backup
sha256sum -c /backups/docmind-YYYYMMDDTHHMMSSZ.dump.sha256
pg_restore --list /backups/docmind-YYYYMMDDTHHMMSSZ.dump >/dev/null

# 2. Restaurar sobre una base de datos limpia de recuperación
pg_restore --clean --if-exists --no-owner -h <HOST_RESTORE> -U <USER> -d docmind_restore /backups/docmind-YYYYMMDDTHHMMSSZ.dump

# 3. Ejecutar reconciliación entre base de datos y objetos almacenados
docker compose exec api python -c "from app.reconcile import reconcile; print(reconcile())"
```

### Evidencia de restauración real ejecutada en entorno de pruebas (2026-11-16)
Sobre contenedor PostgreSQL 17 (`docmind-pg17-rls`) con esquema migrado hasta `0012_saas_plans_intelligence (head)` y datos de 2 organizaciones:
1. `sh /backup-scripts/pg_backup.sh --once` → `backup ok /tmp/backups/docmind-20261116T211713Z.dump 111981 bytes`
2. `sha256sum -c /tmp/backups/*.sha256` → `/tmp/backups/docmind-20261116T211713Z.dump: OK`
3. `createdb -U postgres docmind_restore && pg_restore -U postgres -d docmind_restore --no-owner /tmp/backups/docmind-*.dump` → código de salida `0`
4. `psql -U postgres -d docmind_restore -At -c 'SELECT count(*) FROM organizations;'` → `2` organizaciones íntegras restauradas en base de datos separada.

### Objetivos RPO y RTO recomendados
- **RPO recomendado**: <= 24 h con el intervalo por defecto (`BACKUP_INTERVAL_SECONDS=86400`), o <= 1 h configurando `BACKUP_INTERVAL_SECONDS=3600` (o PITR continuo con WAL archiving en PostgreSQL gestionado).
- **RTO recomendado**: <= 60 min para base de datos + servicio API + reindexado de Qdrant (~31 min cada 10 000 documentos con `scripts/reindex_vectors.py`). *Nota: estos valores son objetivos operativos recomendados; el RTO final en la infraestructura de producción debe medirse sobre el hardware definitivo del propietario.*

---

## 3. Monitoreo y Alertas (`deploy/prometheus/alerts.yml` y `alertmanager.yml`)

### Reglas configuradas en `deploy/prometheus/alerts.yml`
1. `DocMindApiDown`: API caída o sin respuesta a `/metrics` durante 2 min (`critical`).
2. `DocMindHigh5xxRate`: tasa de respuestas HTTP 5xx > 5 % en 5 min (`critical`).
3. `DocMindAskLatencyHigh`: percentil 95 de `/api/v1/ask` > 5 s en 10 min (`warning`).
4. `DocMindProcessingFailureSpike`: fallos del worker > 20 % en 15 min (`warning`).
5. `DocMindStuckJobsRecovered`: trabajos atascados recuperados por el scheduler (`warning`).
6. `DocMindVectorDeletionBacklog`: acumulación en outbox de borrado de Qdrant (`warning`).
7. `DocMindRateLimitSpike`: pico de rechazos HTTP 429 (`info`).
8. `DocMindLlmSpendHigh`: gasto estimado de LLM > 20 USD en 24 h (`warning`).
9. `DocMindWebhookDeliveryFailures`: fallos repetidos de entrega de webhooks (`warning`).
10. `DocMindStorageOrphans`: objetos huérfanos detectados en reconciliación (`warning`).
11. `DocMindRetentionPurgeStalled`: sin ejecuciones exitosas de retención en 36 h (`warning`).

### Configuración de destinatarios en Alertmanager sin valores ficticios
La plantilla [`deploy/prometheus/alertmanager.yml`](../deploy/prometheus/alertmanager.yml) toma los destinatarios exclusivamente desde variables de entorno del operador:
- `ALERTMANAGER_SMTP_SMARTHOST`
- `ALERTMANAGER_SMTP_FROM`
- `ALERTMANAGER_SMTP_USER`
- `ALERTMANAGER_SMTP_PASSWORD`
- `ALERTMANAGER_EMAIL_TO`
- `ALERTMANAGER_WEBHOOK_URL`
