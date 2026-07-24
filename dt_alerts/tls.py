"""
Contexto SSL compartido para conexiones salientes de Alertas-DT.

Motivación: en Windows, un antivirus, proxy o CA corporativa puede instalar
su certificado raíz únicamente en el almacén de certificados del sistema
operativo. ``curl.exe`` y PowerShell (Schannel/.NET) confían en ese almacén de
forma nativa, pero el bundle de certificados que trae Python por defecto
(OpenSSL) puede no reflejarlo, produciendo errores de verificación que no
ocurren fuera de Python. Este módulo prioriza el almacén de Windows vía el
paquete ``truststore`` para evitar ese desajuste.

El módulo también puede instalar un opener HTTPS global de ``urllib`` para la
aplicación. De esa forma, los scrapers DT/SII que usan ``urllib.request``
heredan el mismo contexto seguro que SendGrid, sin desactivar la validación.

Este módulo NUNCA desactiva la verificación de certificados, el chequeo de
hostname, ni acepta certificados no confiables. No descarga certificados
automáticamente ni guarda certificados/secretos en la base de datos.
"""

from __future__ import annotations

import os
import platform
import ssl
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import truststore
except ImportError:  # pragma: no cover - truststore es opcional
    truststore = None  # type: ignore[assignment]

BACKEND_TRUSTSTORE = "truststore"
BACKEND_STANDARD = "ssl estándar"


@dataclass(frozen=True)
class TLSBackendInfo:
    """Información no sensible sobre el contexto SSL construido."""

    backend: str
    os_name: str
    ca_bundle_configured: bool
    ca_bundle_label: str
    error: str | None


_urllib_opener_lock = threading.Lock()
_urllib_opener_installed = False
_urllib_backend_info: TLSBackendInfo | None = None


def _sanitize_path_label(path_str: str) -> str:
    """Devuelve solo el nombre del archivo, nunca la ruta completa."""
    if not path_str:
        return ""
    try:
        name = Path(path_str).name
    except (OSError, ValueError):
        return "(ruta inválida)"
    return name or "(ruta inválida)"


def _truststore_available_on_this_os() -> bool:
    return platform.system() == "Windows" and truststore is not None


def build_ssl_context() -> tuple[ssl.SSLContext, TLSBackendInfo]:
    """
    Construye un contexto SSL seguro para las conexiones salientes.

    Reglas:
    - CERT_REQUIRED y check_hostname siempre activos; nunca se desactivan.
    - En Windows, si ``truststore`` está instalado y no hay una CA corporativa
      explícita configurada (TLS_CA_BUNDLE), se usa el almacén de certificados
      del sistema operativo vía ``truststore.SSLContext``.
    - En cualquier otro caso se usa ``ssl.create_default_context()``.
    - TLS_CA_BUNDLE es opcional y debe apuntar a un archivo local existente.
    """
    ca_bundle_raw = os.getenv("TLS_CA_BUNDLE", "").strip()
    ca_bundle_configured = bool(ca_bundle_raw)
    ca_bundle_label = _sanitize_path_label(ca_bundle_raw)
    ca_error: str | None = None

    use_truststore = _truststore_available_on_this_os() and not ca_bundle_configured

    if use_truststore:
        context: ssl.SSLContext = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        backend = BACKEND_TRUSTSTORE
    else:
        context = ssl.create_default_context()
        backend = BACKEND_STANDARD

    # Nunca desactivar verificación de certificados ni chequeo de hostname.
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True

    if ca_bundle_configured:
        ca_path = Path(ca_bundle_raw)
        if not ca_path.is_file():
            ca_error = f"TLS_CA_BUNDLE apunta a un archivo inexistente: {ca_bundle_label}"
        else:
            try:
                context.load_verify_locations(cafile=str(ca_path))
            except (ssl.SSLError, OSError):
                ca_error = (
                    f"No se pudo cargar TLS_CA_BUNDLE ({ca_bundle_label}): "
                    "certificado inválido o ilegible."
                )

    info = TLSBackendInfo(
        backend=backend,
        os_name=platform.system(),
        ca_bundle_configured=ca_bundle_configured,
        ca_bundle_label=ca_bundle_label,
        error=ca_error,
    )
    return context, info


def install_urllib_https_opener() -> TLSBackendInfo:
    """
    Instala una vez un opener HTTPS seguro para ``urllib.request.urlopen``.

    Los scrapers usan ``urllib.request`` sin pasar un contexto explícito. Este
    opener hace que listados, detalles y PDFs utilicen el contexto construido
    por :func:`build_ssl_context`, incluido el almacén de confianza de Windows
    mediante ``truststore``. Los llamados que ya pasan ``context=`` (como
    SendGrid) conservan su propio contexto.

    Si TLS_CA_BUNDLE está mal configurado se falla de forma explícita en vez de
    continuar con una confianza distinta a la solicitada.
    """
    global _urllib_opener_installed, _urllib_backend_info

    with _urllib_opener_lock:
        if _urllib_opener_installed and _urllib_backend_info is not None:
            return _urllib_backend_info

        context, info = build_ssl_context()
        if info.error:
            raise RuntimeError(info.error)

        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context)
        )
        urllib.request.install_opener(opener)

        _urllib_opener_installed = True
        _urllib_backend_info = info
        return info
