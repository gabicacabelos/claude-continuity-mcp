"""
Ubicar la transcripción de Claude Code de un proyecto — stdlib únicamente, sin efectos en disco.

Claude Code guarda cada sesión como `<projects>/<carpeta-del-proyecto>/<session_id>.jsonl`.
Cómo se arma el nombre de esa carpeta no está documentado: `encode_project_dir` replica lo que
se observa (cada carácter no alfanumérico pasa a `-`), pero si algún día cambia, `find_transcript`
no depende de ello: recorre las demás carpetas y reconoce el proyecto por el `cwd` que cada
transcripción trae en sus primeras líneas.

Nada de acá levanta excepciones: ante rutas inexistentes, archivos ilegibles o entradas raras
se devuelve None.
"""

import json
import os
import re
from itertools import islice
from pathlib import Path

# El recorrido de respaldo mira como mucho esta cantidad de archivos (los más nuevos) por carpeta
# y esta cantidad de líneas por archivo: alcanza para dar con el `cwd` sin leer transcripciones enteras.
SCAN_FILES_PER_FOLDER = 20
SCAN_LINES_PER_FILE = 50


def encode_project_dir(path: str | Path) -> str:
    """Nombre de carpeta que usa Claude Code para un proyecto: cada carácter no alfanumérico pasa a `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_projects_dir() -> Path:
    """`$CLAUDE_CONFIG_DIR/projects` si la variable tiene valor (vacía o solo espacios cuenta como no definida); si no, `~/.claude/projects`."""
    config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(config) if config else Path.home() / ".claude") / "projects"


def _norm(path: str) -> str:
    """Forma comparable de una ruta: normalizada y, en Windows, sin diferencias de mayúsculas."""
    return os.path.normcase(os.path.normpath(path))


def _top_level_jsonl(folder: Path, session_id: str | None) -> list[tuple[float, Path]]:
    """
    `(mtime, ruta)` de los `*.jsonl` de nivel superior de `folder` (las subcarpetas como `subagents/`
    no cuentan), del más nuevo al más viejo. Con `session_id`, solo `<session_id>.jsonl`.
    """
    try:
        candidates = list(folder.glob("*.jsonl")) if session_id is None else [folder / f"{session_id}.jsonl"]
    except OSError:
        return []
    found = []
    for path in candidates:
        try:
            if path.is_file():
                found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return found


def _has_cwd(path: Path, target: str) -> bool:
    """True si alguna de las primeras líneas de `path` es un objeto JSON cuyo `cwd` equivale a `target` (ya normalizado)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in islice(fh, SCAN_LINES_PER_FILE):
                try:
                    entry = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                cwd = entry.get("cwd") if isinstance(entry, dict) else None
                if isinstance(cwd, str) and cwd and _norm(cwd) == target:
                    return True
    except OSError:
        pass
    return False


def _scan_by_cwd(projects: Path, project_dir: str, session_id: str | None) -> Path | None:
    """Red de seguridad: la transcripción más nueva, de cualquier carpeta de `projects`, cuyo `cwd` es `project_dir`."""
    target = _norm(project_dir)
    try:
        folders = sorted(d for d in projects.iterdir() if d.is_dir())
    except OSError:
        return None
    best: tuple[float, Path] | None = None
    for folder in folders:
        for mtime, path in _top_level_jsonl(folder, session_id)[:SCAN_FILES_PER_FOLDER]:
            if best is not None and mtime <= best[0]:
                break  # el resto de la carpeta es más viejo todavía
            if _has_cwd(path, target):
                best = (mtime, path)
                break
    return best[1] if best else None


def find_transcript(
    project_dir: str | Path,
    session_id: str | None = None,
    projects_dir: str | Path | None = None,
) -> Path | None:
    """
    Transcripción `.jsonl` de la sesión de Claude Code en `project_dir`, o None si no se encuentra.

    Primero busca en `projects_dir / encode_project_dir(project_dir)`: sin `session_id`, el `.jsonl` más
    nuevo por mtime; con `session_id`, exactamente `<session_id>.jsonl`. Si ahí no hay nada, recorre las
    demás carpetas de `projects_dir` y elige la más nueva cuyo `cwd` (en sus primeras líneas) sea `project_dir`;
    con `session_id` ese recorrido solo mira archivos con ese nombre. `projects_dir` por defecto es
    `claude_projects_dir()`. Solo cuentan los `.jsonl` de nivel superior de cada carpeta.
    """
    try:
        project = str(project_dir)
        if not project.strip():
            return None
        # `session_id` viene del usuario (flag del CLI): solo un nombre de archivo, nunca una ruta
        if session_id is not None and (not session_id or Path(session_id).name != session_id):
            return None
        projects = Path(projects_dir) if projects_dir is not None else claude_projects_dir()
        direct = _top_level_jsonl(projects / encode_project_dir(project), session_id)
        if direct:
            return direct[0][1]
        return _scan_by_cwd(projects, project, session_id)
    except Exception:  # noqa: BLE001 - contrato del módulo: nunca levanta (lo llaman hooks)
        return None
