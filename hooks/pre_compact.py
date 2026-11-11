#!/usr/bin/env python3
"""
Escudo de compactación, parte 1 — hook PreCompact de Claude Code.

Antes de que Claude Code compacte el contexto, guarda un checkpoint automático
(últimos pedidos, archivos editados, tareas abiertas) extraído de la
transcripción. Ver router/compaction.py.

Regla de oro (igual que capture_read.py): nunca romper la sesión del usuario.
Ante cualquier error sale con 0 y sin escribir a stderr.

Instalación: viene incluido en el plugin (hooks/hooks.json). A mano, en
.claude/settings.json:
  "PreCompact": [{"hooks": [{"type": "command",
      "command": "python /ruta/a/claude-continuity-mcp/hooks/pre_compact.py"}]}]
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> None:
    from router.compaction import extract_session_state, save_compaction_checkpoint
    from router.metrics import Metrics
    from router.paths import resolve_data_dir

    event = json.loads(sys.stdin.read())
    transcript, session_id = event.get("transcript_path"), event.get("session_id")
    if not transcript or not session_id or not Path(transcript).is_file():
        return
    data_dir = resolve_data_dir(REPO)
    state = extract_session_state(transcript)
    name = save_compaction_checkpoint(state, data_dir / "checkpoints", session_id,
                                      trigger=event.get("trigger") or "auto")
    m = Metrics(str(data_dir / "cache" / "metrics.db"))
    m.record("compaction_shielded", ref=name)
    m.close()
    print(json.dumps({"systemMessage": f"claude-continuity: checkpoint '{name}' saved before compaction "
                                       f"({len(state['prompts'])} requests, {len(state['edited'])} edited, "
                                       f"{len(state['read'])} read, {len(state['todos'])} open tasks)"}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
