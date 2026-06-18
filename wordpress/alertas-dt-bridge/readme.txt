=== Alertas DT Bridge ===
Contributors:       externalgroup
Tags:               alertas, dt, dirección del trabajo, suscripción, email
Requires at least:  6.0
Tested up to:       6.7
Requires PHP:       8.0
Stable tag:         0.1.0
License:            GPL-2.0-or-later
License URI:        https://www.gnu.org/licenses/gpl-2.0.html

Formulario de suscripción Alertas DT y API REST para sincronización con app Python local.

== Descripción ==

Este plugin permite capturar suscriptores en WordPress mediante un shortcode y expone una API REST privada para que la app Python local (Alertas DT) sincronice esos suscriptores a su base SQLite y envíe alertas por email con SendGrid.

Arquitectura recomendada:

1. WordPress público captura suscriptores con [alertas_dt_form].
2. App Python local consulta /wp-json/alertas-dt/v1/subscribers.
3. App Python local monitorea la Dirección del Trabajo y envía alertas por SendGrid.

No se envían correos desde WordPress.
No se hace scraping desde WordPress.
La app local no queda expuesta a internet.

== Instalación ==

1. Sube la carpeta `alertas-dt-bridge` a `wp-content/plugins/`.
2. Activa el plugin en "Plugins" de WordPress.
3. Ve a "Alertas DT" en el menú de administración.
4. Copia el shortcode `[alertas_dt_form]` y pégalo en cualquier página.
5. Copia el token API y configura la app Python local.

== Uso del shortcode ==

Básico:
[alertas_dt_form]

Con source_page personalizado:
[alertas_dt_form source_page="home"]

== API REST ==

GET  /wp-json/alertas-dt/v1/health          (público)
GET  /wp-json/alertas-dt/v1/subscribers     (protegido)
POST /wp-json/alertas-dt/v1/subscribers/synced (protegido)

Autenticación: Authorization: Bearer TOKEN

== Configuración app Python ==

WORDPRESS_SYNC_ENABLED=true
WORDPRESS_API_URL=https://tu-sitio.cl/wp-json/alertas-dt/v1
WORDPRESS_API_TOKEN=token-desde-admin-wordpress
WORDPRESS_SYNC_INTERVAL_MINUTES=15
WORDPRESS_SYNC_LIMIT=100

== Changelog ==

= 0.1.0 =
* Primera versión: shortcode, tabla propia, API REST, admin settings.
