# Alertas DT para Contadores

SaaS externo (Python estándar + SQLite) que monitorea normativa de la Dirección del
Trabajo, genera resúmenes orientados a contadores y empresas, y prepara alertas por
email. Se integra a WordPress mediante formulario embebible o iframe. Diseño visual
basado en el sistema **External Group**.

> **Estado:** MVP funcional para revisión interna. No vender como 100% productivo.
> WhatsApp, pagos y planes comerciales quedan reservados para fases futuras.

---

## Estado del MVP

**Qué funciona hoy**

- Landing pública con diseño External Group.
- Suscripción por **email** (validación, consentimiento obligatorio, sin duplicados).
- Panel admin protegido por token: métricas reales, suscriptores, documentos, alertas y jobs.
- Scraper/monitor DT por URL canónica `w3-article-XXXXX.html`, robusto ante fallas por fuente.
- Generación de alertas con resumen, puntos clave, impacto práctico y relevancia (IA opcional + fallback local).
- Vista previa de email (HTML + texto) desde el admin.
- Envío transaccional preparado con **SendGrid** y **modo simulado/console** seguro.
- Acción "Enviar prueba" y flujo de revisión → envío a suscriptores activos.

**Qué NO está activo en esta fase**

- **WhatsApp** (reservado para fase futura; oculto en el formulario).
- Pagos y planes comerciales.
- Migración a Postgres (se usa SQLite).
- Cron productivo externo (si no se configura, el worker interno corre cada N horas).
- Envío real de email si faltan credenciales (queda simulado/`skipped`).

---

## UX/UI de la demo

La interfaz incluye:

- Landing pública para suscripción por email, con beneficios y "cómo funciona".
- Formulario embebible para WordPress.
- Panel administrativo con métricas, suscriptores, documentos, alertas y jobs.
- Vista previa de email (HTML + texto).
- Estados visuales para revisión, envío simulado y credenciales pendientes.
- Microcopy en español (estados técnicos traducidos), responsive y foco accesible.

## Estado de envío de email

Por defecto, la app usa:

```env
EMAIL_PROVIDER=console
```

Esto permite probar la interfaz y registrar envíos **simulados** sin enviar correos reales.

Para envío real en Render:

```env
EMAIL_PROVIDER=sendgrid
SENDGRID_API_KEY=...
EMAIL_FROM=alertasdt@externalgroup.cl
```

(No se incluyen credenciales reales en el repositorio; se definen solo en Render.)

## Ejecutar localmente

```bash
python app.py
```

Rutas:

- Formulario: `http://localhost:8000/`
- Embed (iframe): `http://localhost:8000/embed`
- Admin: `http://localhost:8000/admin/login`
- Healthcheck: `http://localhost:8000/healthz`

Token admin por defecto en desarrollo: `dev-admin-token`. En producción define `ADMIN_TOKEN` y `JOB_TOKEN`.

---

## Rutas principales

```text
GET   /                         Landing + formulario
GET   /embed                    Formulario para iframe
GET   /thanks                   Confirmación de suscripción
GET   /healthz                  Healthcheck JSON
GET   /admin/login              Login admin (token)
GET   /admin                    Resumen (jobs + alertas recientes)
GET   /admin/subscribers        Suscriptores
GET   /admin/documents          Documentos detectados
GET   /admin/alerts             Alertas
GET   /admin/alerts/{id}/preview-email   Vista previa del email
POST  /api/subscribe            Alta/actualización de suscriptor
POST  /api/jobs/check-dt        Ejecuta el monitoreo (requiere X-Job-Token)
POST  /admin/subscribers/{id}/pause|reactivate
POST  /admin/documents/{id}/regenerate|ignore
POST  /admin/alerts/{id}/ready|send|test
```

---

## Variables de entorno

```env
APP_HOST=0.0.0.0
APP_PORT=10000
APP_BASE_URL=
ADMIN_TOKEN=
JOB_TOKEN=
DISABLE_ADMIN_AUTH=False

# Email
EMAIL_PROVIDER=console        # console | sendgrid | resend | smtp
SENDGRID_API_KEY=
EMAIL_FROM=
EMAIL_FROM_NAME=Alertas DT
EMAIL_REPLY_TO=
TEST_EMAIL_TO=

# IA (opcional). Sin clave, el resumen usa fallback local y queda pending_review.
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

# Worker
RUN_WORKER=True
RUN_ON_STARTUP=False
CHECK_INTERVAL_HOURS=6
ALERT_ON_FIRST_RUN=False
```

### Seguridad del admin (`DISABLE_ADMIN_AUTH`)

- **Por defecto `False`** (y también `False` si la variable no existe): el admin exige
  `ADMIN_TOKEN` (login en `/admin/login`).
- `DISABLE_ADMIN_AUTH=True` es **solo para desarrollo local o una demo controlada**:
  omite el login y muestra un banner "Modo desarrollo: autenticación admin desactivada".
- **No usar `DISABLE_ADMIN_AUTH=True` en Render producción.** Producción debe quedar con
  `DISABLE_ADMIN_AUTH=False` (o sin la variable) y un `ADMIN_TOKEN` largo.
- Para la demo con César se usa **login con `ADMIN_TOKEN`** (admin no abierto).

---

## Cómo probar email simulado

1. Deja `EMAIL_PROVIDER=console` (valor por defecto).
2. En el admin (`/admin/alerts`), usa **Vista previa** para ver el email.
3. Usa **Enviar prueba** (con un correo o `TEST_EMAIL_TO`): se registra como
   `simulated` y se imprime en consola, sin enviar nada real.
4. Marca una alerta como **lista** y luego **Enviar**: se simula el envío a los
   suscriptores activos y se registran las `deliveries`.

Ningún correo real sale sin credenciales explícitas.

## Cómo configurar SendGrid (cuando se quiera enviar real)

1. Crear una **API key** en SendGrid (permiso Mail Send).
2. Verificar el **sender** (dominio o remitente único) que usarás en `EMAIL_FROM`.
3. En **Render → Environment**, definir las variables (referencia; la API key se pega
   solo en Render, nunca en el repositorio):
   ```env
   EMAIL_PROVIDER=sendgrid
   SENDGRID_API_KEY=            # se define en Render, no en el código
   EMAIL_FROM=alertasdt@externalgroup.cl
   EMAIL_FROM_NAME=Alertas DT
   EMAIL_REPLY_TO=contacto@externalgroup.cl
   TEST_EMAIL_TO=
   ```
4. Probar con **Enviar prueba** desde el admin hacia tu propio correo.
5. Si falta `SENDGRID_API_KEY`, la app no falla: registra `skipped_missing_credentials`.

> La app lee estas variables **solo desde el entorno** (`os.getenv`). No hay `.env`
> versionado ni credenciales en el código; los secretos viven únicamente en Render.

> Compatibilidad: si ya usabas `RESEND_API_KEY` o `SMTP_*`, puedes apuntar
> `EMAIL_PROVIDER=resend` o `EMAIL_PROVIDER=smtp`. SendGrid es el recomendado.

---

## Primer escaneo

Por seguridad, `ALERT_ON_FIRST_RUN=false` por defecto: el primer monitoreo guarda una
línea base de documentos ya publicados y no genera una avalancha de alertas antiguas.
Desde el siguiente escaneo se procesan documentos nuevos como `pending_review`.

## Ejecutar el job manualmente

```bash
python -m dt_alerts.worker
```

O por HTTP:

```bash
curl -X POST http://localhost:8000/api/jobs/check-dt -H "X-Job-Token: <JOB_TOKEN>"
```

> Las alertas nuevas quedan **pendientes de revisión**: el envío a suscriptores es
> manual desde el admin, nunca automático.

---

## WordPress

Usa el shortcode/snippet de `wordpress/shortcode-snippet.php` o un iframe:

```html
<iframe src="https://alertas-dt.onrender.com/embed"
        width="100%" height="460" style="border:0;" loading="lazy"></iframe>
```

---

## Tests

```bash
python -m unittest
```

Cubren: parseo del listado DT, suscripción (consentimiento, email inválido, dedup/
actualización), auth admin por defecto, render de email HTML/texto y asunto, modos de
envío (console/sendgrid sin clave) y robustez del job (sin documentos / error de fuente
/ duplicados).

## Aviso

El resumen es informativo y no reemplaza la revisión profesional ni la lectura del
documento oficial de la Dirección del Trabajo.
