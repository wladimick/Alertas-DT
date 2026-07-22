import os

# Debe establecerse antes de que cualquier módulo de la suite importe
# dt_alerts.config: evita cargar secretos reales de .env.local/.env durante
# las pruebas automatizadas.
os.environ.setdefault("ALERTAS_DT_SKIP_DOTENV", "1")
