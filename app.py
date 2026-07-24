import logging

from dt_alerts.tls import install_urllib_https_opener


_tls_info = install_urllib_https_opener()
logging.getLogger("dt_alerts.tls").info(
    "TLS urllib configurado: backend=%s os=%s ca_bundle=%s",
    _tls_info.backend,
    _tls_info.os_name,
    _tls_info.ca_bundle_configured,
)

# Importar el servidor después de instalar el opener TLS garantiza que todos
# los módulos de scraping que usan urllib hereden el contexto seguro.
from dt_alerts.server import run_server  # noqa: E402


if __name__ == "__main__":
    run_server()
