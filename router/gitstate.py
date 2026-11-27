"""
Estado de git de solo lectura para el handoff — stdlib únicamente, sin dependencias.

El handoff necesita saber en qué rama está el proyecto, qué falta commitear y si hay
commits sin subir. Eso se lee con `git`, nunca se escribe: la única excepción es
`add_local_exclude`, que agrega una línea a `.git/info/exclude` (el ignore local que
no se commitea) para que la carpeta del handoff no ensucie `git status`.

Nada de acá levanta excepciones: sin git, fuera de un repo, con un timeout o ante
cualquier error se devuelve `{"available": False}`, `None` o `False`.
"""

import os
import re
import subprocess
from pathlib import Path

DEFAULT_MAX_FILES = 30
DEFAULT_TIMEOUT = 5.0


def _git(cwd, *args, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """
    Corre `git <args>` en `cwd` y devuelve stdout, o None si falla (código ≠ 0, timeout,
    git no instalado, carpeta inexistente). GIT_OPTIONAL_LOCKS=0 evita que `status` tome
    el lock del índice y le moleste a otro git corriendo; LC_ALL=C fija el formato de salida.
    """
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(cwd), env=env, shell=False, timeout=timeout,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.stdout if done.returncode == 0 else None


def project_root(cwd: str | Path | None) -> Path | None:
    """Raíz del repo que contiene `cwd`, o None si no es un repo, no hay git o `cwd` es None."""
    if cwd is None:
        return None
    out = _git(cwd, "rev-parse", "--show-toplevel")
    top = out.rstrip("\r\n") if out else ""
    return Path(top) if top else None


def _parse_header(header: str) -> tuple[str | None, str | None, int, int]:
    """
    Interpreta el encabezado de `status -b` (sin el "## "): `main...origin/main [ahead 1, behind 2]`,
    `main`, `No commits yet on main` o `HEAD (no branch)`. Devuelve (rama, upstream, ahead, behind).
    """
    track = ""
    if header.endswith("]") and " [" in header:
        header, _, track = header.rpartition(" [")
        track = track[:-1]
    for prefix in ("No commits yet on ", "Initial commit on "):
        if header.startswith(prefix):
            header = header[len(prefix):]
    if header.startswith("HEAD (no branch)"):
        return None, None, 0, 0
    branch, _, upstream = header.partition("...")
    ahead = re.search(r"ahead (\d+)", track)
    behind = re.search(r"behind (\d+)", track)
    return (branch or None, upstream or None,
            int(ahead.group(1)) if ahead else 0, int(behind.group(1)) if behind else 0)


def git_state(project_dir: str | Path, max_files: int = DEFAULT_MAX_FILES,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """
    Estado del repo de `project_dir`:
    {"available": True, "root", "branch", "upstream", "ahead", "behind", "head", "subject",
     "dirty", "dirty_total"}, o {"available": False} si no hay repo, git o algo falla.
    `branch` es None con HEAD suelto; `head` y `subject`, sin commits; ahead/behind valen 0 sin
    upstream. `dirty` lista hasta `max_files` rutas (relativas al repo, con "/"); `dirty_total`
    cuenta todas. Las carpetas sin trackear aparecen colapsadas ("carpeta/").
    """
    root = project_root(project_dir)
    if root is None:
        return {"available": False}
    status = _git(root, "status", "--porcelain=v1", "-b", "-z", timeout=timeout)
    if status is None:
        return {"available": False}

    # Con -z los registros van separados por NUL y las rutas no se escapan. El primero es el
    # encabezado; en las entradas renombradas o copiadas el registro siguiente es la ruta de origen.
    records = status.split("\0")
    branch = upstream = None
    ahead = behind = 0
    if records and records[0].startswith("## "):
        branch, upstream, ahead, behind = _parse_header(records.pop(0)[3:])
    dirty: list[str] = []
    total = 0
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4 or rec.startswith("!!"):
            continue
        if "R" in rec[:2] or "C" in rec[:2]:
            i += 1
        total += 1
        if len(dirty) < max_files:
            dirty.append(rec[3:])

    head = subject = None
    log = _git(root, "log", "-1", "--format=%h%x00%s", timeout=timeout)
    if log and "\0" in log:
        head, _, subject = log.rstrip("\r\n").partition("\0")
        head, subject = head or None, subject or None
    return {"available": True, "root": str(root), "branch": branch, "upstream": upstream,
            "ahead": ahead, "behind": behind, "head": head, "subject": subject,
            "dirty": dirty, "dirty_total": total}


def add_local_exclude(root: str | Path, entry: str) -> bool:
    """
    Agrega `entry` a `info/exclude` del repo (el ignore local, que no se commitea) una sola vez.
    True si la línea quedó presente; False si `root` no es un repo o algo falla.
    """
    entry = entry.strip()
    if not entry or "\n" in entry:
        return False
    out = _git(root, "rev-parse", "--git-path", "info/exclude")
    rel = out.rstrip("\r\n") if out else ""
    if not rel:
        return False
    path = Path(rel)
    if not path.is_absolute():
        path = Path(root) / path
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if entry in (line.strip() for line in text.splitlines()):
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        # Si la última línea no termina en salto, se lo agrega antes para no pegar las entradas.
        prefix = "\n" if text and not text.endswith("\n") else ""
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"{prefix}{entry}\n")
    except OSError:
        return False
    return True
