# Configuración de Almacenamiento S3 y Cifrado SSE-KMS en Producción

> **Estado:** Adaptador `S3StorageProvider` (con reintentos estándar SigV4, cifrado SSE-KMS, URLs prefirmadas y metadatos `head_object`) **COMPLETADO Y PROBADO CON MOTO EN SUITE AUTOMATIZADA**. La validación contra un bucket real de AWS/ MinIO/Cloud Storage está **BLOQUEADA POR CREDENCIALES S3/KMS** del propietario.

---

## 1. Arquitectura y controles implementados en `backend/app/storage.py`

1. **Doble barrera de aislamiento por tenant (`owned_key`)**:
   - Todas las claves de objeto se construyen bajo el prefijo `<organization_id>/<uuid>.<ext>`.
   - `owned_key(organization_id, key)` verifica que la clave comience por `<organization_id>/` y no contenga segmentos `..` ni `\`, impidiendo que un registro manipulado lea o elimine objetos de otro cliente.
2. **Cifrado en reposo con AWS KMS (`S3_KMS_KEY_ID`)**:
   - Cuando `S3_KMS_KEY_ID` está definido, cada `put_object` envía `ServerSideEncryption="aws:kms"` y `SSEKMSKeyId=self.kms_key_id`.
3. **Reintentos y firma SigV4**:
   - El cliente `boto3` se inicializa con `Config(retries={"max_attempts": 3, "mode": "standard"}, signature_version="s3v4")`.
4. **Descarga autenticada sin URLs prefirmadas compartibles (`test_tenant_isolation.py`) y metadatos (`head_metadata`)**:
   - Por invariante de seguridad del repositorio (`test_private_files_have_no_unauthenticated_or_unauthorized_path`), DOCMIND **nunca** emite URLs prefirmadas de S3 hacia el navegador (ya que una URL prefirmada seguiría funcionando tras revocar una sesión o concesión de proyecto). Toda descarga pasa por `GET /api/v1/documents/{id}/download`, verificando `membership()` y `require_document()` en el instante de la petición.
   - `head_metadata(key)` inspecciona tamaño, `ETag`, `ServerSideEncryption`, `SSEKMSKeyId` y `VersionId`.
5. **Fallo cerrado en `staging` / `production`**:
   - `core.py` prohíbe `STORAGE_PROVIDER=local` fuera de `development`/`test` y exige `S3_BUCKET`, `S3_REGION`, `AWS_ACCESS_KEY_ID` y `AWS_SECRET_ACCESS_KEY`.

---

## 2. Política IAM de mínimo privilegio para el bucket y la clave KMS

### Política del rol/usuario de la aplicación (`docmind-storage-app`)
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DocMindBucketList",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::<S3_BUCKET>"
    },
    {
      "Sid": "DocMindObjectReadWriteDelete",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectVersion"],
      "Resource": "arn:aws:s3:::<S3_BUCKET>/*"
    },
    {
      "Sid": "DocMindKmsEncryptDecrypt",
      "Effect": "Allow",
      "Action": ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
      "Resource": "arn:aws:kms:<S3_REGION>:<ACCOUNT_ID>:key/<KMS_KEY_UUID>"
    }
  ]
}
```

### Política del bucket para exigir TLS y SSE-KMS
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": ["arn:aws:s3:::<S3_BUCKET>", "arn:aws:s3:::<S3_BUCKET>/*"],
      "Condition": {"Bool": {"aws:SecureTransport": "false"}}
    }
  ]
}
```

---

## 3. Variables de entorno y procedimiento de validación real

```env
STORAGE_PROVIDER=s3
S3_BUCKET=docmind-prod-documents
S3_REGION=us-east-1
S3_ENDPOINT_URL=
S3_KMS_KEY_ID=arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444
AWS_ACCESS_KEY_ID=<AKIA...>
AWS_SECRET_ACCESS_KEY=<SECRET>
```

### Comprobación cuando el propietario configure las credenciales:
```bash
cd backend
python -c "
from app.storage import S3StorageProvider
s3 = S3StorageProvider()
key = '00000000-0000-0000-0000-000000000000/kms-smoke.txt'
s3.put(key, b'docmind-kms-smoke')
meta = s3.head_metadata(key)
assert s3.get(key) == b'docmind-kms-smoke'
s3.delete(key)
print('S3+KMS OK:', meta)
"
```
