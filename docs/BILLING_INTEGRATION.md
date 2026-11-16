# Integración Comercial de Billing y Pasarelas de Pago (Chile y Global)

> **Estado:** Arquitectura de adaptadores (con llamadas HTTP reales vía `httpx` para Stripe, Mercado Pago y Transbank cuando `BILLING_LIVE_ENABLED=true`, más modo Sandbox determinista por defecto), máquina de estados normalizada, idempotencia, conciliación, morosidad (dunning) y webhooks firmados **COMPLETADOS Y PROBADOS CON `MockTransport` Y EN SANDBOX**. La activación de cobros reales (`BILLING_LIVE_ENABLED=true`) contra cuentas comerciales está **BLOQUEADA POR CREDENCIALES COMERCIALES** del propietario.

---

## 1. Análisis comparativo para operación desde Chile

No todas las pasarelas ofrecen las mismas capacidades técnicas ni comerciales en Chile:

| Pasarela | Suscripciones nativas | Reembolsos API | Monedas soportadas | Ventajas en Chile | Limitaciones reales | Recomendación |
|---|---|---|---|---|---|---|
| **Stripe** (`stripe`) | Sí (`Subscription` + `Checkout`) | Sí | `USD`, `CLP` | Mejor máquina de estados SaaS B2B, dunning automático, webhooks firmados `Stripe-Signature`, cobro a tarjetas internacionales y chilenas Visa/Mastercard | No procesa débito bancario Redcompra local sin tarjeta internacional habilitada | **Recomendada como pasarela primaria B2B** (clientes corporativos e internacionales en USD/CLP) |
| **Mercado Pago** (`mercadopago`) | Sí (`Preapproval`) | Sí | `CLP`, `USD` | Alta penetración en Chile/LATAM, cobro recurrente en CLP, billetera local y tarjetas nacionales | Conciliación de preapprovals tiene menor flexibilidad de prorrateo al cambiar de plan que Stripe | **Recomendada como pasarela secundaria local en CLP** para PYMEs chilenas |
| **Transbank Webpay Plus / Oneclick Mall** (`transbank`) | **No** (requiere inscripción `Oneclick Mall` + cobros iniciados por el comercio) | Sí (`nullify` / reversa con ventana horaria) | `CLP`, `UF` | Estándar bancario nacional en Chile (Redcompra débito + crédito en cuotas) | Webpay Plus es transaccional; las suscripciones recurrentes exigen contrato comercial **Oneclick Mall** con Transbank y motor propio de cargos periódicos | **Recomendada para contratos Enterprise / Corporativos en Chile** que exigen Redcompra o facturación en UF |
| **Sandbox determinista** (`sandbox`) | Sí | Sí | `USD`, `CLP`, `UF` | Sin llamadas de red externas; paridad completa de estados y firmas HMAC para CI/E2E y previsualización | No ejecuta cobros reales | **Activa por defecto en `development` y `test`** |

---

## 2. Arquitectura desacoplada implementada

El módulo [`backend/app/billing_providers.py`](../backend/app/billing_providers.py) define el protocolo `PaymentProviderAdapter` y cuatro adaptadores (`SandboxPaymentAdapter`, `StripeAdapter`, `MercadoPagoAdapter`, `TransbankAdapter`), consumidos por [`backend/app/routers/billing.py`](../backend/app/routers/billing.py) y [`backend/app/plans.py`](../backend/app/plans.py).

### Máquina de estados normalizada (`OrganizationSubscription.status`)

Todos los estados externos de cada pasarela se normalizan a los 7 estados canónicos internos:

- `trialing`: periodo de prueba activo.
- `active`: suscripción al día; aplica todos los límites del plan contratado.
- `past_due`: 1 o 2 intentos de cobro fallidos; mantiene servicio mientras alerta al administrador (`failed_payment_count` 1..2).
- `unpaid`: 3 o más cobros fallidos consecutivos (morosidad); degrada automáticamente los límites efectivos al plan `FREE` hasta regularizar pago.
- `canceled`: suscripción cancelada; aplica límites `FREE`.
- `incomplete`: checkout iniciado pendiente de confirmación por webhook.
- `expired`: sesión o periodo expirado sin pago; aplica límites `FREE`.

### Tablas con Row-Level Security (migración `0012_saas_plans_intelligence`)

1. `organization_subscriptions`: estado normalizado, ciclo (`monthly`/`annual`), `payment_provider`, `external_customer_id`, `external_subscription_id`, `failed_payment_count`, `cancel_at_period_end`, `trial_ends_at`, `custom_limits`.
2. `billing_transactions`: historial de transacciones (`checkout`, `renewal`, `plan_change`, `refund`, `payment_succeeded`, `payment_failed`) con restricción única `(organization_id, idempotency_key)`.
3. `billing_webhook_events`: registro inmutable de eventos recibidos con restricción única `(provider, event_id)` para deduplicación estricta.

---

## 3. Endpoints comerciales y de webhooks

| Método y Ruta | Permiso | Función |
|---|---|---|
| `GET /api/v1/plans` | Público | Catálogo de planes configurables (`FREE`, `STARTER`, `PROFESSIONAL`, `BUSINESS`, `ENTERPRISE`) |
| `GET /api/v1/billing/providers` | `usage:read` / miembro | Catálogo de pasarelas, monedas y estado (`live_charges_enabled`) |
| `GET /api/v1/billing/subscription` | `Role.admin` | Estado detallado de la suscripción de la organización |
| `PATCH /api/v1/billing/subscription` | `Role.owner` | Actualización administrativa/sandbox de plan, ciclo o umbral de alerta |
| `POST /api/v1/billing/checkout-preview` | `Role.owner` | Simulación de importe y límites antes de iniciar checkout |
| `POST /api/v1/billing/checkout` | `Role.owner` | Creación idempotente de cliente + sesión de checkout en la pasarela |
| `POST /api/v1/billing/subscription/cancel` | `Role.owner` | Cancelación inmediata o al final del periodo (`cancel_at_period_end`) |
| `POST /api/v1/billing/refund` | `Role.owner` | Reembolso total o parcial de una transacción `succeeded` |
| `GET /api/v1/billing/transactions` | `Role.admin` | Historial de transacciones y eventos de webhook del tenant |
| `POST /api/v1/billing/reconcile` | `Role.owner` | Conciliación entre `OrganizationSubscription` y `Organization.plan` |
| `POST /api/v1/billing/webhooks/{provider}` | Firma HMAC | Recepción autenticada de eventos (`payment.succeeded`, `payment.failed`, `subscription.created`, `subscription.renewed`, `subscription.canceled`, `subscription.plan_changed`, `payment.refunded`, `payment_method.updated`) |

### Seguridad de Webhooks

1. **Verificación criptográfica HMAC-SHA256**: cabecera `X-Billing-Signature`, `Stripe-Signature` o `X-Signature` en formato `t=<unix_ts>,v1=<hex_hmac>`.
2. **Protección contra Replay**: rechaza con `400 WEBHOOK_TIMESTAMP_OUT_OF_RANGE` cualquier evento fuera de la ventana `BILLING_WEBHOOK_TOLERANCE_SECONDS` (300 s por defecto).
3. **Idempotencia**: si `(provider, event_id)` ya existe en `billing_webhook_events`, devuelve `200 {"received": true, "duplicate": true}` sin volver a aplicar efectos.
4. **El frontend nunca confirma pagos**: la redirección del navegador tras el checkout es meramente informativa; solo el webhook firmado activa o renueva la suscripción en producción.

---

## 4. Variables de entorno para activar producción

```env
BILLING_PROVIDER=stripe                  # sandbox | stripe | mercadopago | transbank
BILLING_LIVE_ENABLED=false               # Cambiar a true SOLO con credenciales reales en producción
BILLING_WEBHOOK_SECRET=<secreto-hmac>    # Obligatorio cuando BILLING_LIVE_ENABLED=true
BILLING_WEBHOOK_TOLERANCE_SECONDS=300
STRIPE_SECRET_KEY=<sk_live_...>          # Obligatorio si BILLING_PROVIDER=stripe
MERCADOPAGO_ACCESS_TOKEN=<APP_USR-...>   # Obligatorio si BILLING_PROVIDER=mercadopago
TRANSBANK_API_KEY=<tbk-api-key>          # Obligatorio si BILLING_PROVIDER=transbank
TRANSBANK_COMMERCE_CODE=<codigo-comercio> # Código de comercio Transbank (producción)
PLANS_CONFIG_JSON=                       # JSON opcional para sobrescribir precios/límites sin redesplegar
```
