#!/usr/bin/env python3
"""
Autoguardado del handoff — hooks Stop y StopFailure de Claude Code.

  Stop         al terminar cada turno guarda un checkpoint `autosave-<sesión>` (a lo sumo uno
               por minuto y sesión). No escribe nada en el proyecto.
  StopFailure  cuando un error de la API corta la sesión (límite de uso, sobrecarga) guarda el
               checkpoint sin límite de frecuencia y deja además `.claude-continuity/HANDOFF.md`
               en la raíz del proyecto, para retomar en otra cuenta o en otro agente.

Todo se extrae de la transcripción y de git, sin resúmenes de un LLM. Se apaga con
CLAUDE_CONTINUITY_AUTOSAVE=0 o con `"autosave": false` en router_config.json. Ver router/handoff.py.

Regla de oro (igual que pre_compact.py): nunca romper la sesión del usuario. Ante cualquier error
sale con 0, y no imprime nada, ni a stdout ni a stderr.

Instalación: viene incluido en el plugin (hooks/hooks.json). A mano, en .claude/settings.json:
  "Stop": [{"hooks": [{"type": "command",
      "command": "python /ruta/a/claude-continuity-mcp/hooks/autosave.py"}]}]
y lo mismo bajo "StopFailure".
"""

import contextlib
import io
import json
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _text(value) -> str:
    """El valor si es un texto con contenido; si no, "" (un evento puede traer cualquier tipo)."""
    return value if isinstance(value, str) and value.strip() else ""


def main() -> None:
    from router import handoff
    from router.gitstate import project_root
    from router.paths import resolve_data_dir
    from router.safety import is_allowed
    from router.transcripts import find_transcript

    # Claude Code manda el evento en UTF-8; `sys.stdin.read()` usaría la codificación del sistema (cp1252 en
    # Windows) y desfiguraría las rutas con tildes o eñes
    event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    kind = event.get("hook_event_name") if isinstance(event, dict) else None
    if kind not in ("Stop", "StopFailure"):
        return
    data_dir = resolve_data_dir(REPO)
    config = handoff.read_hook_config(data_dir)
    if not config["autosave"]:
        return

    project_dir = _text(event.get("cwd")) or os.getcwd()
    event_session = _text(event.get("session_id"))
    transcript = _text(event.get("transcript_path"))
    if not transcript or not Path(transcript).is_file():
        # solo un corte por error busca la transcripción por su cuenta; en un Stop normal no hay nada que rescatar
        found = None
        if kind == "StopFailure":
            found = (find_transcript(project_dir, session_id=event_session) if event_session else None) \
                or find_transcript(project_dir)
        if found is None:
            return
        transcript = str(found)
    session_id = event_session or Path(transcript).stem

    if kind == "Stop" and not handoff.should_autosave(data_dir / "checkpoints", session_id):
        return
    if config["allowed_roots"]:
        root = project_root(project_dir) or Path(project_dir)
        if not is_allowed(root, config["allowed_roots"]):
            return

    if kind == "Stop":
        handoff.capture(transcript, project_dir, session_id, "turn_end", data_dir, write_file=False)
    else:
        handoff.capture(transcript, project_dir, session_id, str(event.get("error") or "api_error"),
                        data_dir, write_file=True)


if __name__ == "__main__":
    try:
        # Las librerías del repo registran avisos (un archivo de reglas corrupto, el ledger) y el logger
        # de último recurso de Python los imprimiría en stderr: se apaga el logging y, por si algo escribe
        # directo, stdout y stderr van a un buffer descartable.
        logging.disable(logging.CRITICAL)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main()
    except Exception:
        pass
    sys.exit(0)
