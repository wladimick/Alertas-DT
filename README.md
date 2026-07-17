# Alertas DT + SII

Servicio de monitoreo normativo para contadores y empresas. Revisa publicaciones de
la Dirección del Trabajo (DT) y del Servicio de Impuestos Internos (SII), detecta
documentos nuevos, genera resúmenes con impacto práctico y administra el envío de
alertas por email.

La solución incluye una app Python con SQLite, un panel administrativo y un plugin
bridge para integrar el formulario de suscripción con WordPress. La interfaz y las
plantillas de correo usan el sistema visual de External Group.

> **Estado actual:** versión funcional integrada. Antes de usarla en producción deben
> configurarse persistencia, credenciales, dominio remitente y ejecución programada.
> WhatsApp está preparado a nivel de datos y consentimiento, pero requiere Meta
> WhatsApp Business, credenciales y una plantilla aprobada.

## Funcionalidades

- Monitoreo independiente o conjunto de fuentes DT y SII.
- Detección de duplicados por URL canónica e identificador oficial.
- Extracción de título, fecha, categoría, abstract, detalle y contenido PDF cuando
  está disponible.
- Resumen local de respaldo o resumen con OpenAI/Azure AI Foundry.
- Resumen ejecutivo, puntos clave, impactos, acciones recomendadas, relevancia y
  aviso legal.
- Registro de uso de IA, límites diarios/mensuales y estimación de costo en USD/CLP.
- Suscripción por email, WhatsApp opcional, consentimiento y actualización sin
  duplicados.
- Panel para administrar suscriptores, planes, documentos, alertas, jobs e
  integraciones.
- Revisión manual de alertas antes de enviarlas.
- Vista previa HTML/texto, correo de prueba y envío masivo a suscriptores activos.
- Email transaccional con SendGrid y plantillas compatibles con Outlook.
- Sincronización privada de suscriptores desde WordPress.

No están incluidos los pagos, la facturación ni la activación comercial de planes.

## Arquitectura

```text
WordPress público
  `- Plugin [alertas_dt_form]
       |- guarda suscriptores en WordPress
       `- expone API REST privada con Bearer token
                         |
                         v
Servicio Python (local o cloud)
  |- sincroniza suscriptores desde WordPress
  |- monitorea DT + SII cada 6 horas
  |- extrae paginas y PDF
  |- genera resumen local o con IA
  |- guarda documentos, alertas, envios y errores en SQLite
  `- envía email con SendGrid
```

La app no necesita recibir conexiones desde WordPress: es la app la que consulta la
API privada del plugin. Si se despliega en cloud, se recomienda HTTPS, una base de
datos o disco persistente y un cron externo.

## Fuentes monitoreadas

### Dirección del Trabajo

- Portada de normativa.
- Resoluciones.
- Dictámenes.
- Órdenes de Servicio.
- Circulares.
- Ordinarios.
- Resumen de Jurisprudencia Administrativa.

### Servicio de Impuestos Internos

- Circulares del año configurado.
- Resoluciones del año configurado.
- Jurisprudencia administrativa de Renta.
- Jurisprudencia administrativa de IVA.
- Jurisprudencia administrativa de otras normas.

`SII_YEAR` usa el año actual por defecto y permite consultar otro período.

## Flujo de operación

1. El worker revisa las 12 fuentes configuradas.
2. Los documentos nuevos se guardan sin duplicar URL o identificador.
3. Se extrae el contenido de la página y del PDF cuando corresponde.
4. Se genera una alerta con fallback local o IA.
5. La alerta queda en `pending_review`; no se envía automáticamente.
6. Un administrador revisa, genera o regenera el resumen y lo marca como listo.
7. El administrador envía una prueba o la alerta a los suscriptores activos.
8. Cada entrega queda registrada con estado y detalle de error.

Con `ALERT_ON_FIRST_RUN=false`, el primer escaneo solo crea una línea base y evita
enviar normativa histórica.

## Panel administrativo

El panel usa sidebar y vistas operativas:

- **Resumen** (`/admin`): métricas, estado del sistema y siguiente acción.
- **Suscriptores** (`/admin/subscribers`): activar, pausar, eliminar y asignar plan.
- **Documentos** (`/admin/documents`): botones `Todos`, `DT` y `SII`, fuente visible,
  regeneracion e ignorado de documentos.
- **Alertas** (`/admin/alerts`): tabla con filtros por estado, paginación, vista
  previa, prueba, envío masivo y eliminación.
- **Monitoreo** (`/admin/jobs`): historial de ejecuciones y errores por fuente.
- **Configuración** (`/admin/settings`): SendGrid, WordPress, Azure/OpenAI, uso de
  tokens, costo estimado y configuración editorial.

La barra superior permite ejecutar `Actualizar todo`, `Actualizar DT` o
`Actualizar SII`. El acceso exige `ADMIN_TOKEN`, salvo que se habilite explícitamente
el modo de desarrollo.

## Ejecutar localmente

Requiere Python 3.11 o superior.

```bash
python -m venv .venv
pip install -r requirements.txt
python app.py
```

En Windows se puede activar el entorno con:

```powershell
.\.venv\Scripts\Activate.ps1
```

Rutas locales:

- Formulario: `http://localhost:8000/`
- Embed para iframe: `http://localhost:8000/embed`
- Login admin: `http://localhost:8000/admin/login`
- Healthcheck: `http://localhost:8000/healthz`

Los valores locales por defecto son solo para desarrollo. En cualquier ambiente
compartido se deben definir `ADMIN_TOKEN` y `JOB_TOKEN` largos y distintos.

## Variables de entorno

La aplicación lee la configuración con `os.getenv`. Los secretos deben definirse en
el servicio cloud, contenedor o proceso; no deben agregarse al repositorio.

```env
# Aplicación y seguridad
APP_HOST=0.0.0.0
APP_PORT=8000
APP_BASE_URL=https://alertas.example.com
ADMIN_TOKEN=
JOB_TOKEN=
DISABLE_ADMIN_AUTH=false
DATABASE_PATH=data/alertas_normativas.sqlite3

# Monitoreo
RUN_WORKER=true
RUN_ON_STARTUP=false
CHECK_INTERVAL_HOURS=6
MAX_LISTING_DOCUMENTS_PER_SOURCE=25
ALERT_ON_FIRST_RUN=false
SII_YEAR=2026

# Email: console | sendgrid | resend | smtp
EMAIL_PROVIDER=console
SENDGRID_API_KEY=
EMAIL_FROM=alertas@example.com
EMAIL_FROM_NAME=Alertas DT + SII
EMAIL_REPLY_TO=
TEST_EMAIL_TO=

# IA: disabled | openai | azure
AI_ENABLED=false
AI_PROVIDER=disabled
AI_API_KEY=
AI_MODEL=
AI_BASE_URL=
AI_SUMMARY_TEMPERATURE=0.2
AI_TIMEOUT_SECONDS=60
AI_MAX_INPUT_CHARS=45000
AI_ATTACHMENTS_ENABLED=true

# Límites y costo referencial de IA
AI_DAILY_TOKEN_LIMIT=50000
AI_MONTHLY_TOKEN_LIMIT=500000
AI_WARNING_PERCENT=80
AI_INPUT_PRICE_PER_1M_USD=2.00
AI_OUTPUT_PRICE_PER_1M_USD=8.00
AI_USD_CLP_RATE=921

# WordPress
WORDPRESS_SYNC_ENABLED=false
WORDPRESS_API_URL=https://tu-sitio.cl/wp-json/alertas-dt/v1
WORDPRESS_API_TOKEN=
WORDPRESS_SYNC_INTERVAL_MINUTES=15
WORDPRESS_SYNC_LIMIT=100

# WhatsApp Business Cloud API
WHATSAPP_ENABLED=false
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_TEMPLATE_NAME=alerta_normativa
WHATSAPP_LANGUAGE=es
```

### Seguridad del admin

- `DISABLE_ADMIN_AUTH=false` exige login con `ADMIN_TOKEN`.
- `DISABLE_ADMIN_AUTH=true` se reserva para desarrollo local y muestra un aviso en
  el panel.
- Las credenciales de SendGrid, Azure, WordPress y WhatsApp no se guardan en Git.
- La clave de IA se enmascara en el panel y se redacta de los errores registrados.

## Azure AI Foundry

La integración soporta Azure AI Foundry mediante la Responses API v1 y mantiene un
fallback para Azure OpenAI clásico.

```env
AI_ENABLED=true
AI_PROVIDER=azure
AI_API_KEY=
AI_MODEL=nombre-del-deployment
AI_BASE_URL=https://tu-recurso.services.ai.azure.com/openai/v1
```

Para un endpoint Azure OpenAI clásico, `AI_BASE_URL` puede ser la URL base del recurso;
la app construye la ruta de `chat/completions` con el deployment indicado en
`AI_MODEL`.

Desde `/admin/settings` se puede:

- Activar o desactivar la ejecución de IA en tiempo de operación.
- Probar la conexión.
- Revisar tokens de entrada, salida y totales.
- Exportar el historial de uso en CSV.
- Ver límites y costo estimado en USD y CLP.
- Editar instrucciones editoriales y plantillas del resumen.

Si la IA está desactivada, no tiene credenciales, excede los límites o falla, el job
continúa con un resumen local y deja la alerta pendiente de revisión.

## SendGrid

El modo por defecto no envía correos reales:

```env
EMAIL_PROVIDER=console
```

Para envío transaccional:

```env
EMAIL_PROVIDER=sendgrid
SENDGRID_API_KEY=
EMAIL_FROM=alertas@tu-dominio.cl
EMAIL_FROM_NAME=Alertas DT + SII
EMAIL_REPLY_TO=contacto@tu-dominio.cl
TEST_EMAIL_TO=qa@tu-dominio.cl
```

Antes de activar el envío se debe verificar el dominio o remitente en SendGrid. El
panel permite probar la configuración y el correo de cada alerta. Las plantillas usan
tablas HTML para conservar compatibilidad con Outlook e incluyen enlaces oficiales y,
cuando están habilitados, resumen ejecutivo y detallado como adjuntos HTML.

## WordPress

El plugin se encuentra en `wordpress/alertas-dt-bridge/`.

1. Subir la carpeta a `wp-content/plugins/`.
2. Activar **Alertas DT + SII** en WordPress.
3. Insertar el shortcode `[alertas_dt_form]` en la página pública.
4. Copiar el token generado por el plugin.
5. Configurar `WORDPRESS_API_URL` y `WORDPRESS_API_TOKEN` en la app.

Endpoints del plugin:

```text
GET  /wp-json/alertas-dt/v1/health
GET  /wp-json/alertas-dt/v1/subscribers
POST /wp-json/alertas-dt/v1/subscribers/synced
```

Los endpoints de suscriptores requieren `Authorization: Bearer TOKEN`. La
sincronización puede ejecutarse manualmente desde el panel o mediante el scheduler.

## API y rutas principales

```text
GET  /                                  Landing y formulario
GET  /embed                             Formulario embebible
GET  /thanks                            Confirmación de suscripción
GET  /healthz                           Estado del servicio
POST /api/subscribe                     Alta o actualización de suscriptor

POST /api/jobs/check-normative?source=all
POST /api/jobs/check-normative?source=dt
POST /api/jobs/check-normative?source=sii
POST /api/jobs/check-dt
POST /api/jobs/check-sii

GET  /admin
GET  /admin/subscribers
GET  /admin/documents?source=dt|sii
GET  /admin/alerts?status=<estado>&page=<número>
GET  /admin/jobs
GET  /admin/settings
GET  /admin/settings/ai-usage.csv
GET  /admin/alerts/{id}/preview-email

POST /admin/subscribers/{id}/pause|reactivate|activate|delete
POST /admin/subscribers/{id}/plan
POST /admin/documents/{id}/regenerate|ignore
POST /admin/alerts/{id}/ready|send|resend|test|delete
POST /admin/alerts/{id}/generate-ai|regenerate-ai
POST /admin/settings/ai-toggle
```

Los jobs HTTP requieren el header `X-Job-Token`.

Ejemplo:

```bash
curl -X POST "http://localhost:8000/api/jobs/check-normative?source=dt" \
  -H "X-Job-Token: <JOB_TOKEN>"
```

También se puede ejecutar el monitoreo completo con:

```bash
python -m dt_alerts.worker
```

## Persistencia y despliegue

SQLite es suficiente para operación pequeña siempre que el archivo se ubique en un
disco persistente. En servicios con filesystem efímero, configurar por ejemplo:

```env
DATABASE_PATH=/var/data/alertas_normativas.sqlite3
```

Los archivos `data/*.sqlite3`, `.env` y `__pycache__` están excluidos por
`.gitignore`. Para mayor volumen o múltiples instancias se recomienda migrar a
PostgreSQL antes de escalar horizontalmente.

## Tests

```bash
python -m unittest
```

La suite actual contiene 128 pruebas sobre suscripciones, autenticación, scraping DT
y SII, PDF, deduplicación, fallback e IA, costos, email, WordPress, endpoints y flujos
administrativos.

## Aviso legal

Los resúmenes son informativos. No reemplazan la lectura del documento oficial ni la
revisión de un profesional contable, tributario o legal.
