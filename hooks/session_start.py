#!/usr/bin/env python3
"""
Escudo de compactación, parte 2 — hook SessionStart (matcher "compact").

Justo después de compactar, reinyecta en el contexto nuevo lo que pre_compact.py
guardó: pedidos, archivos, tareas abiertas y las reglas del proyecto. Solo
actúa cuando la sesión arranca por compactación; en cualquier otro caso no
imprime nada. Ver router/compaction.py.

Instalación: viene incluido en el plugin (hooks/hooks.json). A mano, en
.claude/settings.json:
  "SessionStart": [{"matcher": "compact", "hooks": [{"type": "command",
      "command": "python /ruta/a/claude-continuity-mcp/hooks/session_start.py"}]}]
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> None:
    from router.compaction import restore_context
    from router.paths import resolve_data_dir

    event = json.loads(sys.stdin.read())
    if event.get("source") != "compact" or not event.get("session_id"):
        return
    context = restore_context(resolve_data_dir(REPO) / "checkpoints", event["session_id"], event.get("cwd"))
    if context:
        # ASCII escapado a propósito: en Windows el stdout de un hook sale en cp1252
        # y los acentos romperían el JSON que lee Claude Code.
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": context}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
