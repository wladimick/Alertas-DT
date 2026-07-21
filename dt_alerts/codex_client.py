"""
Cliente aislado para el proveedor de IA "codex".

Envuelve el SDK oficial `openai-codex` (paquete `openai_codex`), que reutiliza
la sesión de ChatGPT ya autenticada en esta máquina Windows (`codex login`,
sin API key ni AI_BASE_URL). No abre navegador por sí mismo: eso solo ocurre
en scripts/codex_login.py, ejecutado manualmente por el operador.

Cada llamada a run_codex_prompt() crea un cliente y un thread nuevos (uno por
documento, sin historial compartido), en sandbox de solo lectura y con
aprobaciones siempre denegadas, y los cierra siempre al terminar.
"""

from __future__ import annotations

import concurrent.futures
import shutil
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, Settings

CODEX_HOME_DIR = PROJECT_ROOT / ".codex_home"


def is_codex_sdk_available() -> bool:
    try:
        import openai_codex  # noqa: F401
    except ImportError:
        return False
    return True


def _isolated_codex_home() -> Path:
    """CODEX_HOME propio de Alertas-DT: no hereda plugins/mcp_servers del
    Codex/ChatGPT Desktop personal del usuario. Reutiliza el login copiando
    auth.json desde el CODEX_HOME real (~/.codex), sin exponer su contenido."""
    CODEX_HOME_DIR.mkdir(exist_ok=True)
    real_auth = Path.home() / ".codex" / "auth.json"
    isolated_auth = CODEX_HOME_DIR / "auth.json"
    if real_auth.exists() and (
        not isolated_auth.exists()
        or real_auth.stat().st_mtime > isolated_auth.stat().st_mtime
    ):
        shutil.copyfile(real_auth, isolated_auth)
    return CODEX_HOME_DIR


def _make_codex() -> Any:
    """Crea un cliente Codex aislado. Requiere is_codex_sdk_available()."""
    from openai_codex import Codex, CodexConfig

    codex_home = _isolated_codex_home()
    return Codex(config=CodexConfig(env={"CODEX_HOME": str(codex_home)}))


def _safe_error(exc: object) -> str:
    """Convierte una excepción/mensaje del SDK a texto acotado, sin rutas ni tokens."""
    text = str(exc).strip()
    return text[:500] if text else "Error desconocido del SDK de Codex."


def check_login_status() -> tuple[bool, str]:
    """
    Comprueba si hay una sesión de ChatGPT activa para Alertas-DT.
    No abre navegador ni imprime/expone tokens o rutas de auth.json.
    Seguro de llamar desde el servidor, el worker o el botón "Probar conexión".
    """
    if not is_codex_sdk_available():
        return False, "SDK de Codex no instalado (pip install -r requirements.txt)."
    try:
        with _make_codex() as codex:
            response = codex.account()
    except Exception as exc:
        return False, _safe_error(exc)
    if getattr(response, "account", None) is None:
        return False, "No hay sesión de ChatGPT activa. Ejecuta scripts/codex_login.py."
    return True, "Sesión de ChatGPT activa."


def login_with_chatgpt() -> tuple[bool, str]:
    """
    Dispara el login interactivo por navegador (OAuth de ChatGPT).
    Uso exclusivo de scripts/codex_login.py: nunca debe invocarse desde el
    servidor, el worker o cualquier ruta que se ejecute automáticamente.
    """
    if not is_codex_sdk_available():
        return False, "SDK de Codex no instalado (pip install -r requirements.txt)."
    try:
        with _make_codex() as codex:
            handle = codex.login_chatgpt()
            auth_url = getattr(handle, "auth_url", "")
            if auth_url:
                print(f"Si el navegador no se abre solo, visita: {auth_url}")
            result = handle.wait()
            if not getattr(result, "success", False):
                return False, f"Login con ChatGPT falló: {_safe_error(getattr(result, 'error', ''))}"
            return True, "Login confirmado correctamente."
    except Exception as exc:
        return False, _safe_error(exc)


def _run_single_turn(system_prompt: str, user_prompt: str, workdir: Path) -> str:
    from openai_codex import ApprovalMode, Sandbox
    from openai_codex.models import ErrorNotification, ItemCompletedNotification

    with _make_codex() as codex:
        thread = codex.thread_start(
            base_instructions=system_prompt,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            cwd=str(workdir),
        )
        try:
            handle = thread.turn(user_prompt)
            agent_messages: list[str] = []
            for notification in handle.stream():
                payload = notification.payload
                if isinstance(payload, ItemCompletedNotification):
                    item = payload.item.root
                    if item.type == "agentMessage":
                        agent_messages.append(item.text)
                elif isinstance(payload, ErrorNotification):
                    raise RuntimeError(_safe_error(payload.error.message))
            return agent_messages[-1] if agent_messages else ""
        finally:
            close_thread = getattr(thread, "close", None)
            if callable(close_thread):
                close_thread()


def run_codex_prompt(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
) -> tuple[str, int, int, int]:
    """
    Ejecuta un único turno de Codex para un documento y devuelve
    (content, input_tokens, output_tokens, total_tokens).

    - Thread y cliente nuevos por llamada: sin historial compartido entre documentos.
    - Sandbox de solo lectura y aprobaciones denegadas: no escribe archivos,
      no ejecuta comandos, no usa herramientas ni MCP adicionales.
    - Cierra siempre el thread y el cliente, incluso si falla.
    - No requiere AI_API_KEY ni AI_BASE_URL: usa la sesión de ChatGPT del sistema.
    - El SDK de Codex no expone conteo de tokens por turno; se reportan en 0.
    """
    if not is_codex_sdk_available():
        raise RuntimeError(
            "SDK de Codex no disponible. Instala la dependencia con "
            "pip install -r requirements.txt."
        )

    logged_in, status_text = check_login_status()
    if not logged_in:
        raise RuntimeError(
            f"{status_text} Ejecuta scripts/codex_login.py para autenticarte."
        )

    codex_home = _isolated_codex_home()
    workdir = codex_home / "workdir"
    workdir.mkdir(exist_ok=True)

    timeout = max(1, int(getattr(settings, "ai_timeout_seconds", 60) or 60))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_single_turn, system_prompt, user_prompt, workdir)
        try:
            content = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise RuntimeError(f"Codex no respondió dentro de {timeout}s.") from exc

    return content, 0, 0, 0
