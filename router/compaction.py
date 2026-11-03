"""
Escudo de compactación (Claude Code): el estado de la sesión sobrevive a /compact.

La queja #1 de Claude Code: al compactar, el modelo olvida reglas, pedidos y en
qué archivos estaba trabajando. Dos hooks lo cubren sin depender de que el
modelo se acuerde de nada:

  - PreCompact  → extract_session_state() + save_compaction_checkpoint():
                  checkpoint automático desde la transcripción (últimos pedidos,
                  archivos editados, tareas abiertas). Determinista: se extrae,
                  nunca se resume con un LLM.
  - SessionStart(compact) → restore_context(): texto que el hook reinyecta en
                  el contexto nuevo, con las reglas del proyecto incluidas.

El checkpoint es uno común (mismo formato que router_checkpoint): se puede
retomar desde cualquier cliente con action=resume.
"""

import json
import time
from pathlib import Path

from . import rules as project_rules
from .ledger import FileLedger
from .safety import redact_secrets
from .verify import build_anchors

MAX_PROMPTS = 5
MAX_PROMPT_CHARS = 400
MAX_FILES = 15
MAX_AUTO_CHECKPOINTS = 10
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PREFIX = "autocompact-"


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return []


def _is_human_prompt(text: str) -> bool:
    """Lo que escribió la persona — no recordatorios ni etiquetas que inyecta el harness."""
    t = text.strip()
    return bool(t) and not t.startswith("<")


def extract_session_state(transcript_path: str | Path) -> dict:
    prompts: list[str] = []
    edited: list[str] = []
    todos: list[str] = []
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            kind = entry.get("type")
            if kind not in ("user", "assistant"):
                continue
            content = (entry.get("message") or {}).get("content")
            if kind == "user":
                for t in _texts(content):
                    if _is_human_prompt(t):
                        prompts.append(t.strip()[:MAX_PROMPT_CHARS])
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, args = block.get("name", ""), block.get("input") or {}
                if name in EDIT_TOOLS and args.get("file_path"):
                    path = args["file_path"]
                    if path in edited:
                        edited.remove(path)
                    edited.append(path)  # el más reciente al final
                if isinstance(args.get("todos"), list):
                    todos = [t.get("content", "") for t in args["todos"]
                             if isinstance(t, dict) and t.get("status") != "completed" and t.get("content")]
    return {"prompts": prompts[-MAX_PROMPTS:], "edited": edited[-MAX_FILES:], "todos": todos}


def checkpoint_name(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return (PREFIX + safe)[:60]


def _file_hash(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return FileLedger.hash(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def save_compaction_checkpoint(state: dict, checkpoints_dir: str | Path, session_id: str,
                               trigger: str = "auto") -> str:
    cps = Path(checkpoints_dir)
    cps.mkdir(parents=True, exist_ok=True)
    name = checkpoint_name(session_id)
    prompts = [redact_secrets(p)[0] for p in state["prompts"]]
    summary = (f"Checkpoint automático antes de compactar ({trigger}). Últimos pedidos del usuario: "
               + " | ".join(prompts) if prompts else
               f"Checkpoint automático antes de compactar ({trigger}).")
    data = {
        "name": name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M"),
        "ts": time.time(),
        "auto": "pre_compact",
        "summary": summary,
        "decisions": [],
        "open_items": state["todos"],
        "files": [{"path": f, "hash": _file_hash(f)} for f in state["edited"]],
        "anchors": build_anchors([summary, *state["todos"]], state["edited"]),
    }
    (cps / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    autos = sorted(cps.glob(f"{PREFIX}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in autos[MAX_AUTO_CHECKPOINTS:]:
        old.unlink(missing_ok=True)
    return name


def restore_context(checkpoints_dir: str | Path, session_id: str, cwd: str | None) -> str | None:
    path = Path(checkpoints_dir) / f"{checkpoint_name(session_id)}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None

    lines = ["[claude-continuity] Contexto restaurado después de compactar — esto se guardó justo antes:"]
    if data.get("summary"):
        lines.append(data["summary"])
    if data.get("files"):
        lines.append("Archivos que venías editando: " + ", ".join(f["path"] for f in data["files"]))
    if data.get("open_items"):
        lines.append("Tareas abiertas: " + "; ".join(data["open_items"]))
    rules_file = project_rules.find_rules_file(cwd) if cwd else None
    if rules_file:
        texts = project_rules.inject_texts(project_rules.load_rules(rules_file.parent))
        if texts:
            lines.append("Reglas permanentes del proyecto (seguí respetándolas): " + "; ".join(texts))
    lines.append(f"Detalle completo: router_checkpoint action=resume name={data['name']}. "
                 "Para releer esos archivos, router_smart_read devuelve solo lo que cambió.")
    return "\n".join(lines)
