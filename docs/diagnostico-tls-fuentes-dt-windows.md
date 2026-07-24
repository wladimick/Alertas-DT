# Diagnóstico TLS de fuentes DT en Windows

## Síntoma

En la VM Windows Poseidón, todas las fuentes de la Dirección del Trabajo terminaban con:

```text
SSL: CERTIFICATE_VERIFY_FAILED
unable to get local issuer certificate
```

El monitoreo se ejecutaba, pero terminaba `partial`. El SII continuaba operativo y la vista de documentos mostraba documentos SII, pero ningún documento DT.

## Causa

Las solicitudes del scraper se realizan mediante `urllib.request`. Python utiliza OpenSSL y su propio conjunto de autoridades certificadoras, que puede diferir del almacén de confianza de Windows.

En equipos con proxy, antivirus o CA corporativa, la raíz necesaria puede existir en Windows y ser reconocida por Schannel (`curl.exe`, PowerShell o navegadores), pero no por el contexto predeterminado de Python.

## Solución aplicada

La aplicación instala al arrancar un opener HTTPS seguro para `urllib`, construido por `dt_alerts.tls.build_ssl_context()`.

En Windows, cuando `truststore` está disponible y no se definió `TLS_CA_BUNDLE`, el contexto utiliza el almacén de certificados del sistema operativo. El mismo contexto se aplica a:

- listados DT;
- detalles de documentos DT;
- descargas PDF;
- consultas SII basadas en `urllib`.

La solución mantiene obligatoriamente:

```text
verify_mode = CERT_REQUIRED
check_hostname = true
```

No se usa `CERT_NONE`, `ssl._create_unverified_context()` ni otra omisión de la validación SSL.

## Archivos

- `dt_alerts/tls.py`: construcción del contexto e instalación idempotente del opener.
- `app.py`: instala el opener antes de importar e iniciar el servidor.
- `tests/test_dt_tls.py`: valida instalación única y cierre seguro ante una CA inválida.

## Despliegue sin apagar la VM

La corrección puede validarse sin detener la VM ni el túnel actual:

1. Mantener la aplicación productiva temporal en el puerto 8000.
2. Crear un worktree de la rama `fix/dt-scraper-windows-tls`.
3. Copiar la base SQLite a un archivo de prueba.
4. Iniciar la rama en otro puerto, por ejemplo 8001, con:

```text
RUN_WORKER=false
RUN_ON_STARTUP=false
EMAIL_PROVIDER=console
```

5. Ejecutar una única comprobación DT sobre la copia de la base.
6. Confirmar que las fuentes devuelven documentos y que no aparece `CERTIFICATE_VERIFY_FAILED`.
7. Detener solamente la instancia de prueba.
8. En una ventana acordada, reiniciar únicamente el proceso Python del puerto 8000. No detener `cloudflared` ni apagar la VM.

El cambio definitivo puede producir una interrupción breve del panel mientras se reemplaza el proceso Python, pero no requiere reiniciar Windows ni cerrar el túnel.

## Criterios de validación

- El backend TLS informado en Windows es `truststore` o una CA explícita válida.
- `/healthz` responde 200.
- `Actualizar DT` obtiene listados reales.
- El contador DT deja de ser cero.
- El job no contiene errores `CERTIFICATE_VERIFY_FAILED`.
- Los documentos quedan registrados sin duplicar URLs.
- No se envían correos automáticamente; las alertas continúan en revisión manual.
