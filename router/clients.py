"""
Identidad de clientes MCP: el `clientInfo.name` que manda cada agente al
conectarse, normalizado a un id corto (para el inbox) y a un nombre legible
(para los reportes). La memoria es MCP estándar: sirve igual para Claude Code,
Cursor, Codex, Gemini CLI, Copilot o cualquier otro cliente.
"""

import re

# (fragmento del clientInfo.name, id corto, nombre legible) — el orden importa:
# "claude-code" tiene que ganarle a cualquier patrón más genérico.
_KNOWN = [
    ("claude-code", "code", "Claude Code"),
    ("claude-ai", "desktop", "Claude Desktop"),
    ("cowork", "cowork", "Claude Cowork"),
    ("cursor", "cursor", "Cursor"),
    ("codex", "codex", "Codex"),
    ("gemini", "gemini", "Gemini CLI"),
    ("visual studio code", "copilot", "VS Code / Copilot"),
    ("vscode", "copilot", "VS Code / Copilot"),
    ("copilot", "copilot", "VS Code / Copilot"),
    ("windsurf", "windsurf", "Windsurf"),
    ("cline", "cline", "Cline"),
    ("zed", "zed", "Zed"),
    ("goose", "goose", "Goose"),
]


def _match(raw: str) -> tuple[str, str] | None:
    name = raw.lower()
    for fragment, cid, label in _KNOWN:
        if fragment in name:
            return cid, label
    return None


def client_id(raw: str | None) -> str | None:
    if not raw:
        return None
    hit = _match(raw)
    if hit:
        return hit[0]
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:24] or None


def friendly(raw: str | None) -> str | None:
    if not raw:
        return None
    hit = _match(raw)
    return hit[1] if hit else raw
