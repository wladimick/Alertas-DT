# Alertas DT para Contadores

MVP de SaaS externo para WordPress que monitorea normativa de la Dirección del Trabajo, genera resúmenes orientados a contadores y empresas, y envía alertas por email. WhatsApp queda preparado para activarse con WhatsApp Business Cloud API.

## Qué incluye

- Formulario público embebible en WordPress.
- API `POST /api/subscribe`.
- Panel admin para suscriptores, documentos y alertas.
- Worker `POST /api/jobs/check-dt` y scheduler cada 6 horas.
- Scraper de la DT basado en URLs canónicas `w3-article-XXXXX.html`.
- Resumen con OpenAI si `OPENAI_API_KEY` está configurado.
- Fallback local marcado como `pending_review` cuando no hay IA.
- Envío por Resend o SMTP; sin credenciales se registra como simulado.
- WhatsApp preparado y simulado por defecto.

## Ejecutar localmente

```powershell
py app.py
```

Luego abre:

- Formulario: `http://localhost:8000/`
- Embed: `http://localhost:8000/embed`
- Admin: `http://localhost:8000/admin/login`
- Healthcheck: `http://localhost:8000/healthz`

El token admin por defecto en desarrollo es `dev-admin-token`. En producción cambia `ADMIN_TOKEN` y `JOB_TOKEN`.

## Variables de entorno

Copia `.env.example` a `.env` en tu hosting o define esas variables en la plataforma. La app no carga `.env` automáticamente para evitar dependencias externas; puedes usar variables del sistema o tu proveedor de despliegue.

Valores mínimos para producción:

```powershell
$env:ADMIN_TOKEN="token-largo"
$env:JOB_TOKEN="otro-token-largo"
$env:APP_BASE_URL="https://alertas.tudominio.cl"
$env:OPENAI_API_KEY="..."
$env:RESEND_API_KEY="..."
$env:EMAIL_FROM="Alertas DT <alertas@tudominio.cl>"
py app.py
```

## Primer escaneo

Por seguridad, `ALERT_ON_FIRST_RUN=false` por defecto. Eso significa que el primer monitoreo guarda una línea base de documentos ya publicados y no envía una avalancha de alertas antiguas. Desde el siguiente escaneo se procesan y notifican documentos nuevos.

Para forzar alertas en el primer escaneo:

```powershell
$env:ALERT_ON_FIRST_RUN="true"
```

## Ejecutar el job manualmente

```powershell
py -m dt_alerts.worker
```

O por HTTP:

```powershell
Invoke-WebRequest -Method POST `
  -Uri http://localhost:8000/api/jobs/check-dt `
  -Headers @{ "X-Job-Token" = "change-this-job-token" }
```

## WordPress

Usa el shortcode/snippet de `wordpress/shortcode-snippet.php` en un plugin pequeño o en `functions.php`. Luego publica:

```text
[dt_alertas_form base_url="https://alertas.tudominio.cl"]
```

Para una integración rápida también puedes insertar un iframe:

```html
<iframe
  src="https://alertas.tudominio.cl/embed"
  style="width:100%;min-height:560px;border:0;"
  loading="lazy"
></iframe>
```

## Tests

```powershell
py -m unittest
```

## Aviso

El resumen es informativo y no reemplaza revisión profesional ni lectura del documento oficial de la DT.
