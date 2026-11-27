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
import re
import time
from pathlib import Path

from . import rules as project_rules
from .ledger import FileLedger
from .safety import redact_secrets
from .verify import build_anchors

MAX_PROMPTS = 5
MAX_PROMPT_CHARS = 400
MAX_LAST_ASSISTANT_CHARS = 600
MAX_FILES = 15
MAX_AUTO_CHECKPOINTS = 10
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read"}
PREFIX = "autocompact-"


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return []


_HARNESS_BLOCK = re.compile(r"<(system-reminder|command-[\w-]+|local-command-[\w-]+)>.*?</\1>", re.DOTALL)
_PASTED = re.compile(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>", re.DOTALL)


def _human_prompt(text: str) -> str | None:
    """
    Lo que escribió la persona: sin los bloques que inyecta el harness
    (recordatorios, comandos). Lo que pegó sí cuenta — es su pedido.
    """
    t = _HARNESS_BLOCK.sub("", text)
    t = _PASTED.sub(lambda m: "[pasted] " + m.group(1).strip(), t).strip()
    if not t or t.startswith("<"):
        return None
    # primero se enmascara el texto completo: si se cortara antes, una clave que cruza el tope
    # quedaría como un parcial que el patrón ya no reconoce
    return " ".join(redact_secrets(t)[0].split())[:MAX_PROMPT_CHARS]


def extract_session_state(transcript_path: str | Path) -> dict:
    """
    Estado de la sesión desde la transcripción: pedidos, archivos editados y leídos, tareas
    abiertas, el último texto del asistente (`last_assistant`, "" si no hubo) y el último
    `cwd` de nivel superior (`cwd`, None si ninguna línea lo trae).
    """
    prompts: list[str] = []
    edited: list[str] = []
    read: list[str] = []
    todos: list[str] = []
    last_assistant = ""
    cwd: str | None = None
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):  # 123, null, [], "texto": JSON válido pero no un objeto
                continue
            if isinstance(entry.get("cwd"), str) and entry["cwd"].strip():  # un cwd vacío no pisa uno válido
                cwd = entry["cwd"]
            kind = entry.get("type")
            if kind not in ("user", "assistant"):
                continue
            content = (entry.get("message") or {}).get("content")
            if kind == "user":
                for t in _texts(content):
                    prompt = _human_prompt(t)
                    if prompt:
                        prompts.append(prompt)
                continue
            spoken = [t for t in _texts(content) if isinstance(t, str) and t.strip()]
            if spoken:  # un turno solo de herramientas no borra el último texto; `thinking` no cuenta
                last_assistant = spoken[-1]
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, args = block.get("name", ""), block.get("input") or {}
                if name in EDIT_TOOLS and args.get("file_path"):
                    path = args["file_path"]
                    if path in edited:
                        edited.remove(path)
                    edited.append(path)  # el más reciente al final
                elif name in READ_TOOLS and args.get("file_path"):
                    path = args["file_path"]
                    if path in read:
                        read.remove(path)
                    read.append(path)
                if isinstance(args.get("todos"), list):
                    # una sola línea por tarea: con saltos de línea podría fabricar secciones en el handoff
                    todos = [" ".join(t["content"].split()) for t in args["todos"]
                             if isinstance(t, dict) and t.get("status") != "completed"
                             and isinstance(t.get("content"), str) and t["content"].strip()]
    read = [p for p in read if p not in edited]  # lo editado ya figura como editado
    # enmascarar antes de cortar (ver _human_prompt); se hace una sola vez, sobre el último texto
    last_assistant = " ".join(redact_secrets(last_assistant)[0].split())[:MAX_LAST_ASSISTANT_CHARS]
    return {"prompts": prompts[-MAX_PROMPTS:], "edited": edited[-MAX_FILES:],
            "read": read[-MAX_FILES:], "todos": todos,
            "last_assistant": last_assistant, "cwd": cwd}


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
    summary = (f"Automatic checkpoint before compaction ({trigger}). Latest user requests: "
               + " | ".join(prompts) if prompts else
               f"Automatic checkpoint before compaction ({trigger}).")
    data = {
        "name": name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M"),
        "ts": time.time(),
        "auto": "pre_compact",
        "summary": summary,
        "decisions": [],
        "open_items": state["todos"],
        # `files` = editados Y consultados: resume reporta cuáles cambiaron desde entonces
        "files": [{"path": f, "hash": _file_hash(f)} for f in [*state["edited"], *state.get("read", [])]],
        "edited": state["edited"],
        "read": state.get("read", []),
        "anchors": build_anchors([summary, *state["todos"]], [*state["edited"], *state.get("read", [])]),
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

    lines = ["[claude-continuity] Context restored after compaction — saved right before it:"]
    if data.get("summary"):
        lines.append(data["summary"])
    edited = data.get("edited", [f["path"] for f in data.get("files", [])])
    if edited:
        lines.append("Files you were editing: " + ", ".join(edited))
    if data.get("read"):
        lines.append("Files you read: " + ", ".join(data["read"]))
    if data.get("open_items"):
        lines.append("Open tasks: " + "; ".join(data["open_items"]))
    rules_file = project_rules.find_rules_file(cwd) if cwd else None
    if rules_file:
        texts = project_rules.inject_texts(project_rules.load_rules(rules_file.parent))
        if texts:
            lines.append("Permanent project rules (keep following them): " + "; ".join(texts))
    lines.append(f"Full detail: router_checkpoint action=resume name={data['name']}. "
                 "To re-read those files, router_smart_read returns only what changed.")
    return "\n".join(lines)
