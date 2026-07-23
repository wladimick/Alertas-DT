# UI Admin v2 — Alertas y Dashboard operativo

Rama: `feat/alerts-ui-v2`.

## Dashboard operativo

`/admin` prioriza acciones operativas por sobre el detalle técnico extenso, que
se movió a `/admin/jobs` (Monitoreo). El objetivo es que un operador sepa, de
un vistazo, qué requiere su atención y si alguna integración tiene un problema
real vigente.

### Indicadores superiores

Las tarjetas KPI existentes (Suscriptores activos, Documentos detectados,
Pendientes de revisión, Listas para enviar, Alertas enviadas, Envíos
registrados) ahora son clickeables cuando existe una ruta o filtro real:

- Suscriptores activos → `/admin/subscribers`
- Documentos detectados → `/admin/documents`
- Pendientes de revisión → `/admin/alerts?status=pending_review`
- Listas para enviar → `/admin/alerts?status=ready`
- Alertas enviadas → `/admin/alerts?status=sent`

No se inventó ningún estado nuevo: los filtros usan exactamente las claves de
estado (`status`) que ya existen en la tabla `alerts`.

### Prioridad de alertas: "Alertas que requieren atención"

Reemplaza el bloque "Siguiente acción recomendada". Muestra como máximo **5**
alertas, filtradas a los estados que realmente requieren una acción
administrativa: `error`, `fallback`, `pending_review`, `ready`/`ready_to_send`.

Las alertas con estado `sent` **nunca** aparecen en este bloque — para eso
existe el KPI "Alertas enviadas" y el filtro correspondiente en `/admin/alerts`.

Orden de prioridad (implementado en `_dashboard_attention_alerts`):

1. `error`
2. `fallback`
3. `pending_review`
4. `ready` / `ready_to_send`

Cada fila muestra: título del documento, fuente (badge DT/SII reutilizado de
`/admin/documents`), categoría, badge de estado (el mismo componente que usa
`/admin/alerts`) y fecha corta. El botón "Ver" abre la vista previa del correo
de esa alerta.

El enlace "Ver todas las alertas" apunta a `/admin/alerts` y conserva el filtro
de estado cuando **todas** las alertas mostradas comparten el mismo estado;
si hay una mezcla de estados, enlaza a la vista sin filtrar.

Si no hay alertas que requieran atención, se muestra exactamente:
`No hay alertas que requieren atención.`

### Salud compacta: "Salud del sistema"

Reemplaza el bloque técnico "Estado del sistema" (que mostraba excepciones
completas y ocupaba demasiado espacio). Muestra un resumen de 6 integraciones:

- SendGrid
- WordPress Sync
- Proveedor de IA efectivo (usa `get_effective_ai_provider` / selector de
  proveedor de `feat/ai-provider-selector`)
- Monitoreo DT
- Monitoreo SII
- Acceso administrativo

Cada fila usa el mismo componente `pill()` que el resto del panel, con 4
colores semánticos:

| Estado visual | Significado |
|---|---|
| Verde (`active`) | Operativo / conectado / resuelto |
| Amarillo (`warning`) | Advertencia o parcial |
| Rojo (`error`) | Error activo |
| Gris (`disabled` / `nodata`) | Desactivado o sin datos aún |

Una conclusión general resume el estado global:

- **Todo operativo** — ninguna fila en advertencia o error.
- **N integraciones requieren atención** — hay al menos una fila en error.
- **Monitoreo parcial** — no hay errores, pero sí advertencias.
- **Sistema en modo seguro** — nada en error/advertencia, pero el proveedor de
  IA efectivo está desactivado a propósito (fallback local únicamente).

### Tratamiento de errores históricos (activo vs. resuelto vs. sin datos)

Un error nunca debe parecer vigente si ya se resolvió. La regla aplicada:

- **Error activo**: el resultado más reciente para esa integración/fuente
  sigue siendo un error (o, en el caso de IA, el último registro en
  `ai_usage_logs` es un error sin una llamada `success` posterior — ver
  `db.has_ai_success_after`, ya usado en `/admin/settings` desde
  `fix/ai-provider-selector`).
- **Resuelto**: existe una ejecución/llamada exitosa posterior al último
  error registrado. Se muestra en verde con el detalle "Resuelto: hubo un
  intento exitoso después del error (fecha)".
- **Sin datos**: todavía no existe ningún registro para esa integración
  (por ejemplo, SendGrid configurado pero sin ningún envío intentado aún, o
  monitoreo DT/SII que nunca se ha ejecutado).

Ningún registro histórico se borra: la distinción activo/resuelto/sin datos es
puramente de presentación, calculada a partir de `ai_usage_logs` y
`job_runs` tal como ya existen.

### Errores técnicos: resumen sanitizado, no la excepción completa

Antes, un error SSL extenso (con múltiples líneas de traceback) rompía la
jerarquía visual del dashboard. Ahora, cualquier texto de error mostrado en
"Salud del sistema" pasa por `_sanitize_error_text()`, que:

1. Colapsa espacios/saltos de línea.
2. Acota el texto a ~140 caracteres, agregando "…" si se corta.

El texto completo del error sigue disponible sin pérdida en `/admin/jobs`
(columna "Error" del historial de jobs) y en `ai_usage_logs` — el dashboard
solo agrega, junto al resumen, un enlace **"Ver en Monitoreo →"** hacia
`/admin/jobs` para el detalle completo.

### Último monitoreo

Bloque compacto con la fecha/hora de la última ejecución de monitoreo, el
resultado (`Completo` / `Parcial` / `Error` / `En curso`) y "Fuentes exitosas:
X de Y" (Y = fuentes configuradas para ese tipo de job — DT, SII o ambas; X se
calcula descontando las fuentes que aparecen en el texto de error de esa
ejecución). Incluye el enlace **"Ver historial de monitoreo"** hacia
`/admin/jobs`, donde vive el detalle completo sin resumir.

### Responsive

- **Escritorio**: KPIs arriba; debajo, grilla de dos columnas
  (`.eg-dashboard-grid`) con "Alertas que requieren atención" (~65%) y "Salud
  del sistema" (~35%); "Último monitoreo" ocupa el ancho completo debajo.
- **Tablet y móvil** (`≤1024px`): una sola columna. Gracias al orden del HTML,
  "Alertas que requieren atención" aparece primero y "Salud del sistema"
  después, sin necesidad de CSS adicional para reordenar. Las tablas usan su
  propio contenedor con scroll horizontal (`.eg-table-wrap`), por lo que la
  página nunca necesita scroll horizontal completo.

### Consistencia visual

El dashboard reutiliza exactamente los mismos componentes que `/admin/alerts`:
`pill()` / `badge()` para estados, `source_badge()` para DT/SII, `.eg-btn` para
botones, `.eg-card`/`.eg-panel` para tarjetas y la misma tipografía. No se creó
un lenguaje visual nuevo.

### Qué no cambió

Esta mejora es visual y de composición de información. No se modificó:
lógica de monitoreo (`worker.py`), generación de IA (`summarizer.py`),
envío de SendGrid (`notifier.py`), WordPress Sync, el esquema de SQLite, los
estados posibles de una alerta, los flujos de envío ni la autenticación admin.
