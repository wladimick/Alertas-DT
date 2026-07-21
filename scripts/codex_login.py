"""
Login manual de la cuenta ChatGPT usada por el proveedor de IA "codex".

Ejecutar una sola vez (o cuando la sesión caduque) desde una consola con
navegador disponible en esta máquina Windows:

    python scripts/codex_login.py

Este script:
- Comprueba si ya existe una sesión de ChatGPT válida para Alertas-DT.
- Si no existe, inicia el login por navegador (OAuth de ChatGPT).
- Informa si la cuenta quedó autenticada, sin mostrar ni registrar tokens,
  contraseñas ni el contenido de auth.json.
- Nunca arranca Alertas-DT (no importa dt_alerts.server ni dt_alerts.worker).

Alertas-DT (server.py / worker.py) nunca dispara este flujo automáticamente:
solo lee el estado de la sesión con codex_client.check_login_status().
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dt_alerts import codex_client  # noqa: E402


def main() -> int:
    if not codex_client.is_codex_sdk_available():
        print(
            "SDK de Codex no instalado.\n"
            "Instala las dependencias con: pip install -r requirements.txt"
        )
        return 1

    logged_in, status_text = codex_client.check_login_status()
    if logged_in:
        print(f"[login] {status_text} No es necesario volver a autenticarse.")
        return 0

    print(f"[login] Estado actual: {status_text}")
    print("[login] Iniciando login con ChatGPT (se abrirá el navegador)...")
    ok, message = codex_client.login_with_chatgpt()
    print(f"[login] {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
