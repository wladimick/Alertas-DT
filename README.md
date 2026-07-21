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
- Resumen local de respaldo o resumen con OpenAI, Azure AI Foundry o Codex
  (cuenta ChatGPT de esta máquina, sin API key).
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
- **Configuración** (`/admin/settings`): SendGrid, WordPress, Azure/OpenAI/Codex, uso
  de tokens, costo estimado y configuración editorial.

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

# IA: disabled | openai | azure | codex
# codex no requiere AI_API_KEY ni AI_BASE_URL: usa la sesión de ChatGPT
# autenticada en esta máquina (ver "Codex con cuenta ChatGPT" más abajo).
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
- La sesión de ChatGPT usada por Codex se guarda en `.codex_home/` (ignorado por
  Git) y nunca se imprime, registra ni expone en el panel o en los logs.

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

## Codex con cuenta ChatGPT

El proveedor `codex` usa la sesión de ChatGPT ya autenticada en esta máquina Windows
(SDK oficial `openai-codex`), en vez de una API key. No requiere `AI_API_KEY` ni
`AI_BASE_URL`. Cada documento se procesa en un thread nuevo, sin historial
compartido entre documentos, con sandbox de solo lectura y sin permisos para
ejecutar comandos ni escribir archivos.

### Instalación de dependencias (Windows)

```powershell
pip install -r requirements.txt
```

### Login inicial (una sola vez)

Se ejecuta manualmente, con navegador disponible en esta máquina. Alertas-DT
nunca dispara este login por sí mismo:

```powershell
python scripts\codex_login.py
```

El script comprueba si ya existe una sesión válida, inicia el login por
navegador si hace falta, e informa si la cuenta quedó autenticada. Nunca
imprime ni guarda tokens, contraseñas o el contenido de `auth.json`.

### Variables de entorno

```env
AI_ENABLED=true
AI_PROVIDER=codex
```

`AI_API_KEY` y `AI_BASE_URL` se dejan vacíos: no se usan con este proveedor.

### Activación y ejecución

1. Configura `AI_ENABLED=true` y `AI_PROVIDER=codex` en `.env`.
2. Ejecuta `python scripts\codex_login.py` si aún no hay sesión activa.
3. Inicia la aplicación normalmente (`python app.py` o el comando habitual).
4. Desde `/admin/settings` puedes revisar el estado de la sesión y usar
   "Probar conexión IA" (nunca abre navegador; solo comprueba la sesión existente).

### Sesión asociada al usuario Windows

La sesión de ChatGPT queda asociada al usuario del sistema operativo que ejecuta
Alertas-DT. Se guarda de forma aislada en `.codex_home/` (dentro del proyecto,
ignorado por Git) y se reutiliza en cada ejecución sin abrir navegador.

### Si la sesión caduca o se alcanza el límite del plan

Si no hay sesión activa, la sesión caducó, se alcanza el límite del plan ChatGPT,
falla el SDK o Codex devuelve una respuesta no parseable, Alertas-DT usa
automáticamente el resumen local de respaldo y continúa funcionando. La alerta
siempre queda `pending_review` y nunca se envía un correo automáticamente.
Vuelve a ejecutar `python scripts\codex_login.py` para renovar la sesión.

### Sin API key

Codex no necesita `AI_API_KEY`: los límites de uso dependen del plan de ChatGPT
(Plus/Pro/Business) asociado a la cuenta autenticada, no de una cuota de API. El
SDK de Codex no siempre entrega un conteo exacto de tokens a la aplicación, por lo
que el costo estimado en el panel puede no aplicar a este proveedor.

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

## Certificados TLS en Windows

Alertas-DT usa un contexto SSL explícito y compartido (`dt_alerts/tls.py`) para las
conexiones salientes de SendGrid, en vez de depender del comportamiento por defecto
de `urllib`. El objetivo es evitar un problema conocido en Windows corporativo: un
antivirus, proxy o CA corporativa puede instalar su certificado raíz únicamente en
el almacén de certificados de Windows. `curl.exe` y PowerShell (Schannel/.NET) confían
en ese almacén de forma nativa, pero el bundle de certificados que trae Python por
defecto puede no reflejarlo, produciendo un error de verificación que **no** ocurre
fuera de Python.

### Error TLS vs. API key inválida — cómo diferenciarlos

- **Error TLS** (`SSL: CERTIFICATE_VERIFY_FAILED`, "self-signed certificate in
  certificate chain", etc.): ocurre *antes* de que SendGrid llegue a leer la
  petición. Nunca viene acompañado de un código HTTP de SendGrid. El panel de
  Alertas-DT lo reporta explícitamente como "Error TLS" e indica el backend usado,
  aclarando que no es un problema de API key.
- **API key inválida** (`HTTP 401`) o **remitente/permiso no autorizado**
  (`HTTP 403`): la conexión TLS ya se completó con éxito; SendGrid respondió con un
  código HTTP de error. Esto se soluciona revisando la API key o los permisos del
  remitente en SendGrid, no la configuración TLS.
- **Payload o remitente inválido** (`HTTP 400`): igual que arriba, la conexión TLS
  funcionó; el problema está en los datos enviados (remitente no verificado,
  formato del cuerpo del correo, etc.).

### Almacén de certificados de Windows (`truststore`)

Por defecto, en Windows, Alertas-DT usa el paquete [`truststore`](https://pypi.org/project/truststore/)
para validar certificados TLS usando el almacén de certificados del propio sistema
operativo (el mismo que usan `curl.exe` y PowerShell), en vez del bundle de
certificados que trae Python. Esto es transparente: no requiere configuración.

- **Nunca** se desactiva la verificación de certificados, el chequeo de hostname,
  ni se acepta un certificado no confiable. `truststore` solo cambia *de dónde* se
  obtiene la lista de autoridades certificadoras confiables.
- En sistemas que no son Windows, o si el paquete `truststore` no está instalado,
  se usa siempre `ssl.create_default_context()` (el comportamiento estándar de
  Python), sin ningún cambio de comportamiento.

### CA corporativa explícita (opcional): `TLS_CA_BUNDLE`

Si tu organización necesita confiar en una CA corporativa específica que aún no
esté en el almacén de certificados de Windows, puedes indicarla explícitamente:

```env
TLS_CA_BUNDLE=C:\ruta\certificado-corporativo.pem
```

- El archivo debe existir localmente; Alertas-DT **nunca** descarga ni genera
  certificados automáticamente.
- Si `TLS_CA_BUNDLE` está configurado, Alertas-DT usa el contexto SSL estándar de
  Python (no `truststore`) para esa CA, porque `truststore` valida siempre contra
  el almacén del sistema operativo e ignora cualquier CA cargada manualmente. Si
  necesitas que el sistema operativo confíe en esa CA (para que `truststore` la
  use también), instálala en el almacén de certificados de Windows
  (Ejecutar → `certlm.msc`, o `certutil -addstore Root certificado-corporativo.pem`
  desde una consola con privilegios).
- Si el archivo indicado en `TLS_CA_BUNDLE` no existe o no es un certificado
  válido, el panel de Configuración muestra un error claro y **nunca** se
  desactiva la validación como solución alterna.

### Cómo pedir el certificado raíz a infraestructura

Si tanto Windows como Python fallan al validar TLS contra SendGrid (es decir, ni
`curl.exe` ni `Invoke-WebRequest` logran completar la conexión), el problema es de
red o de certificados, no de la aplicación:

1. Pide al equipo de infraestructura/seguridad el certificado raíz (`.pem`/`.crt`)
   de la CA corporativa o del antivirus/proxy que inspecciona TLS en esa VM.
2. Instálalo en el almacén de certificados de Windows ("Entidades de certificación
   raíz de confianza"), no solo en la aplicación.
3. Si por política no puede instalarse a nivel de sistema, usa `TLS_CA_BUNDLE`
   como alternativa explícita solo para Alertas-DT.
4. Nunca se debe pedir "desactivar la verificación SSL" como solución: no está
   soportado ni disponible en esta aplicación.

### Diagnóstico en el panel

`/admin/settings`, dentro de la tarjeta de SendGrid, muestra sin información
sensible: el backend TLS en uso (`truststore` o `ssl estándar`), el sistema
operativo, si `TLS_CA_BUNDLE` está configurado (solo el nombre de archivo, nunca
la ruta completa que podría contener el nombre de usuario de Windows) y el
resultado sanitizado de la última prueba de SendGrid.

### Migrar Alertas-DT a otra máquina virtual Windows

1. Instalar Python 3.11+ y crear el entorno virtual (`python -m venv .venv`).
2. `pip install -r requirements.txt` (incluye `truststore`).
3. Copiar `.env` (o `.env.local`) con las variables reales; nunca versionarlo.
4. Ejecutar `curl.exe -Iv https://api.sendgrid.com` y `Invoke-WebRequest` para
   confirmar que Windows valida TLS correctamente contra SendGrid antes de
   configurar la aplicación.
5. Si esa VM tiene una CA corporativa distinta, seguir la sección anterior
   ("Cómo pedir el certificado raíz a infraestructura") antes de dar por
   resuelto cualquier error TLS.

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
