"""Dónde vive la memoria — compartido por el server y por los hooks."""

import os
from pathlib import Path


def resolve_data_dir(code_dir: Path) -> Path:
    """
    Instalado con uvx/pipx, el código queda en un venv descartable: guardar la
    memoria ahí la perdería en cada upgrade. Orden:
      1. CLAUDE_CONTINUITY_HOME si está seteada.
      2. Junto al código si ya existe cache/ ahí (instalaciones git-clone previas
         a v3.5 — no les movemos la memoria de lugar).
      3. ~/.claude-continuity
    """
    env = os.getenv("CLAUDE_CONTINUITY_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if (code_dir / "cache").is_dir():
        return code_dir
    return Path.home() / ".claude-continuity"
