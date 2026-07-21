# Diagnóstico: error TLS en SendGrid desde Windows

**Rama:** `fix/sendgrid-windows-tls`
**Fecha:** 2026-07-21
**Entorno:** máquina virtual Windows, Python 3.11.9 en `.venv`, sin proxy configurado.

Este informe es sanitizado: no incluye API keys, tokens, contraseñas, contenido de
correos, certificados privados ni información sensible de proxy.

## 1. Problema reportado

Al probar SendGrid desde la aplicación se reportó:

```
SSL: CERTIFICATE_VERIFY_FAILED
certificate verify failed: self-signed certificate in certificate chain
```

El endpoint usado es `https://api.sendgrid.com/v3/mail/send`, implementado en
`dt_alerts/notifier.py` con `urllib.request.urlopen()` sin contexto SSL explícito.

## 2. Pruebas realizadas y resultados

| # | Prueba | Resultado |
|---|--------|-----------|
| 1 | `python --version` / `.venv\Scripts\python.exe --version` | Python 3.11.9 en el venv del proyecto. |
| 2 | `ssl.get_default_verify_paths()` | `cafile=None`, ruta OpenSSL por defecto (`C:\Program Files\Common Files\SSL\cert.pem`) **no existe** en esta VM. |
| 3 | `curl.exe -Iv https://api.sendgrid.com` | TLS negociado vía Schannel, `HTTP 404` (respuesta HTTP válida, no es fallo TLS). |
| 4 | PowerShell `Invoke-WebRequest https://api.sendgrid.com` | `HTTP 404` (TLS OK). |
| 5 | `urllib` con `ssl.create_default_context()` estándar (sin ningún ajuste) | TLS OK, `HTTP 404`. Python cargó 37 CAs (incluye almacén de Windows). |
| 6 | Réplica exacta del POST real a `/v3/mail/send` (con `Authorization` inválida a propósito, sin enviar correo real) | TLS OK, `HTTP 401` (esperado; confirma que la ruta exacta del código no falla en TLS). |
| 7 | Inspección del certificado recibido (vía conexión validada, sin desactivar verificación) | Emisor: **GoDaddy Secure Certificate Authority - G2**; subject `*.api.sendgrid.com`. Certificado público legítimo, sin indicios de interceptación corporativa/antivirus. |
| 8 | Variables de entorno de proxy (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, minúsculas) | Ninguna definida. |
| 9 | `netsh winhttp show proxy` | "Acceso directo (sin servidor proxy)". |
| 10 | Registro WinInet (`ProxyEnable`) | `0` (deshabilitado). |
| 11 | Comparación `ssl.create_default_context()` vs `truststore.SSLContext` contra `api.sendgrid.com` | Ambos exitosos, mismo resultado (`HTTP 404`). |

## 3. Causa encontrada

**No fue posible reproducir el error TLS original en esta sesión de diagnóstico.**
Todas las pruebas —incluida la réplica exacta de la llamada real de
`notifier.py`— completaron la negociación TLS correctamente, contra el
certificado público real de SendGrid (GoDaddy G2), sin proxy configurado y sin
señales de interceptación corporativa.

Posibles explicaciones (no verificables con la evidencia disponible):

- El problema fue intermitente (comportamiento heurístico de un antivirus/EDR con
  inspección TLS que no se activó durante este diagnóstico).
- Cambió el estado de la máquina entre el momento del reporte y este diagnóstico
  (actualización de antivirus, reinicio, cambio de red, VPN).
- El error correspondía a un estado puntual ya resuelto por otro medio (ej. una
  política de grupo que instaló un certificado raíz corporativo después del
  primer reporte).

Dado que el patrón de síntoma reportado (`self-signed certificate in certificate
chain` en Python, mientras herramientas nativas de Windows funcionan) es un
problema real y documentado en entornos Windows corporativos —el bundle de
certificados de Python puede no reflejar el almacén de certificados de
Windows que usan `curl.exe`/PowerShell—, se implementó de todas formas una
solución preventiva (ver sección 4), a pedido explícito, en lugar de descartar el
reporte.

## 4. Solución implementada

Se creó `dt_alerts/tls.py` con `build_ssl_context()`:

- **Nunca** desactiva `CERT_REQUIRED` ni el chequeo de hostname.
- En Windows, si el paquete `truststore` está instalado y no hay una CA
  corporativa explícita configurada, usa `truststore.SSLContext`, que valida
  contra el almacén de certificados del propio sistema operativo (el mismo que
  usan `curl.exe` y PowerShell).
- En cualquier otro caso (no Windows, `truststore` no instalado, o
  `TLS_CA_BUNDLE` configurado) usa `ssl.create_default_context()` sin cambios.
- Soporta opcionalmente `TLS_CA_BUNDLE` para una CA corporativa explícita.
  Se valida que el archivo exista antes de cargarlo; nunca se descarga
  automáticamente. Cuando `TLS_CA_BUNDLE` está configurado se usa el contexto
  estándar (no `truststore`), porque `truststore` ignora las CAs cargadas
  manualmente y valida siempre contra el almacén del sistema operativo.
- `dt_alerts/notifier.py` (`_send_sendgrid`) ahora pasa este contexto
  explícitamente a `urllib.request.urlopen(..., context=ssl_context)`, y
  distingue en su mensaje de error un fallo TLS de una API key inválida (HTTP
  401/403) o un payload inválido (HTTP 400).
- `truststore` se agregó como dependencia fija en `requirements.txt`.
- El panel `/admin/settings` (tarjeta SendGrid) muestra el backend TLS en uso,
  el sistema operativo, si `TLS_CA_BUNDLE` está configurado (solo nombre de
  archivo) y el resultado sanitizado de la última prueba SendGrid.

No se modificó ningún otro punto de la aplicación (Azure/OpenAI, WordPress,
scraping) para mantener el alcance acotado al problema reportado.

## 5. Prueba real de envío

Se realizó **un único envío real y controlado** a `wladimickdiaz@gmail.com`
(usando `TEST_EMAIL_TO`), antes de integrar `tls.py` al código (es decir, con el
código original de `notifier.py` sin el contexto SSL explícito):

- **Resultado:** éxito. `status=sent`, `ok=True`, `provider_message_id` recibido
  (equivalente a `HTTP 202` de SendGrid; SendGrid no tiene otro código de éxito en
  este endpoint).
- **Backend TLS usado en esa prueba:** `ssl` estándar de Python (el fix de
  `truststore` aún no estaba integrado en ese momento).
- No se envió a suscriptores ni se usó la función de envío masivo.

Esto confirma que, en el estado actual de esta VM, el envío real a SendGrid
funciona correctamente incluso sin el fix — consistente con no haber podido
reproducir el error original en el diagnóstico (sección 3).

## 6. Limitaciones pendientes

- **El error original no se reprodujo**, por lo que no se puede confirmar con
  evidencia directa que `truststore` sea la causa raíz ni que la solución
  resuelva *ese* incidente específico. La solución se implementó como blindaje
  preventivo ante un patrón de fallo conocido en Windows corporativo, no como
  corrección verificada de un bug reproducido.
- `truststore` ignora cualquier CA cargada manualmente (`TLS_CA_BUNDLE`): si una
  futura VM necesita confiar en una CA corporativa que no está en el almacén de
  Windows, esa CA debe instalarse en el almacén de Windows, o Alertas-DT usará el
  contexto estándar (no `truststore`) para esa CA específica.
- No se probó el comportamiento en un escenario real con antivirus/proxy con
  inspección TLS activa (no se pudo reproducir tal escenario en esta VM).
- La aplicación no carga automáticamente `.env`/`.env.local` (no usa
  `python-dotenv`); las variables deben cargarse en el proceso antes de
  ejecutar `python app.py`.

## 7. Recomendaciones para la VM definitiva

1. Antes de dar por resuelto cualquier error TLS, ejecutar primero
   `curl.exe -Iv https://api.sendgrid.com` e `Invoke-WebRequest` — si ambos
   fallan, el problema es de red/certificados de la VM, no de la aplicación.
2. Mantener `truststore` instalado (ya es dependencia fija) para que Alertas-DT
   use el almacén de certificados de Windows por defecto.
3. Si la organización usa un antivirus, proxy o firewall con inspección TLS,
   solicitar a infraestructura el certificado raíz correspondiente e instalarlo
   en el almacén de certificados de Windows ("Entidades de certificación raíz de
   confianza"), no solo en la aplicación.
4. Usar `TLS_CA_BUNDLE` únicamente como alternativa explícita cuando no sea
   posible instalar la CA a nivel de sistema operativo.
5. Nunca desactivar la verificación TLS como solución, ni en código ni por
   configuración; no está soportado por esta aplicación.
6. Revisar periódicamente si el error vuelve a presentarse de forma intermitente
   y, de ser así, registrar la hora exacta y el estado de la red/VPN/antivirus en
   ese momento para facilitar una futura reproducción.
