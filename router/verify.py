"""
Verificación de memoria: contrasta lo que un checkpoint AFIRMA del código
contra el código actual, antes de que Claude confíe en él.

El problema: un checkpoint dice "la validación está en `auth.py:120`" o "usá
`validate_token()`". Semanas después el código cambió y esa memoria miente —
y el modelo la repite con total seguridad.

  - build_anchors(): al GUARDAR, fotografía cada referencia del texto que hoy
    es verdadera (archivo que existe, texto exacto de la línea citada, archivos
    donde aparece el símbolo).
  - verify_anchors(): al RETOMAR, contrasta cada ancla con el disco y reporta
    file_missing | line_moved | line_changed | symbol_missing.
  - verify_legacy(): checkpoints previos a las anclas → solo existencia de
    rutas absolutas (conservador: sin foto previa no hay forma de comparar más).

Determinista y 100% local. Lo que ya era falso al guardar no se ancla: una
referencia rota de entrada no puede convertirse en una falsa alarma después.
"""

import re
from pathlib import Path

from .safety import is_allowed, is_sensitive, redact_secrets

MAX_ANCHORS = 30
MAX_ISSUES = 10
MAX_FILE_BYTES = 2_000_000
MAX_LINE_CHARS = 200

# Extensiones que cuentan como "ruta de archivo" en texto libre. Sin esto,
# `v3.5.0`, `e.g.` o `agensi.io` se tomarían como archivos.
PATH_SUFFIXES = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".java", ".go", ".rs",
    ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
    ".sh", ".bash", ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".json", ".md", ".rst", ".txt", ".html", ".css", ".scss", ".vue", ".svelte",
}

_BACKTICK = re.compile(r"`([^`\n]{1,300})`")
_LINE_SUFFIX = re.compile(r"^(.*\.[A-Za-z0-9]{1,8}):(\d{1,6})$")
_BARE_PATH = re.compile(
    r"(?<![\w/\\.])((?:[A-Za-z]:)?[\w./\\-]*[\w-]\.[A-Za-z][A-Za-z0-9]{0,7})(?::(\d{1,6}))?(?!\w)"
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?$")
_CAMEL = re.compile(r"[a-z][A-Z]")


def _as_path(span: str) -> tuple[str, str, int | None] | None:
    if "://" in span:
        return None
    m = _LINE_SUFFIX.match(span)
    raw, line = (m.group(1), int(m.group(2))) if m else (span, None)
    if Path(raw).suffix.lower() not in PATH_SUFFIXES:
        return None
    return span, raw, line


def _as_symbol(span: str) -> str | None:
    """Solo identificadores con forma de código: snake_case, camelCase, a.b o f()."""
    if not _IDENT.match(span):
        return None
    name = span.removesuffix("()").split(".")[-1]
    looks_like_code = "_" in name or "()" in span or "." in span or _CAMEL.search(name)
    return name if len(name) >= 4 and looks_like_code else None


def extract_refs(texts: list[str]) -> tuple[list[tuple[str, str, int | None]], list[str]]:
    """(rutas [(ref, ruta, línea|None)], símbolos) mencionados en los textos, sin duplicados."""
    paths: list[tuple[str, str, int | None]] = []
    symbols: list[str] = []
    for text in texts:
        if not text:
            continue
        for span in _BACKTICK.findall(text):
            span = span.strip()
            p = _as_path(span)
            if p:
                paths.append(p)
                continue
            s = _as_symbol(span)
            if s:
                symbols.append(s)
        for m in _BARE_PATH.finditer(_BACKTICK.sub(" ", text)):
            p = _as_path(m.group(0))
            if p:
                paths.append(p)
    return list(dict.fromkeys(paths)), list(dict.fromkeys(symbols))


def _bases(tracked: list[str]) -> list[Path]:
    """Directorios contra los que resolver rutas relativas: los de los archivos del checkpoint y sus ancestros."""
    out: list[Path] = []
    for t in tracked:
        for parent in list(Path(t).parents)[:4]:
            if parent not in out:
                out.append(parent)
    return out


def _resolve(raw: str, tracked: list[str]) -> Path | None:
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p if p.is_file() else None
    norm = raw.replace("\\", "/").lstrip("./")
    for t in tracked:
        t_norm = t.replace("\\", "/")
        if (t_norm == norm or t_norm.endswith("/" + norm)) and Path(t).is_file():
            return Path(t)
    for base in _bases(tracked):
        candidate = base / norm
        if candidate.is_file():
            return candidate
    return None


def _usable(p: Path, allowed_roots) -> bool:
    return not is_sensitive(p) and (not allowed_roots or is_allowed(p, allowed_roots))


def _read_lines(p: Path) -> list[str] | None:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _norm_line(line: str) -> str:
    text, _ = redact_secrets(line.strip())
    return text[:MAX_LINE_CHARS]


def _contains_symbol(p: Path, symbol: str) -> bool:
    lines = _read_lines(p)
    if lines is None:
        return False
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")
    return any(pattern.search(line) for line in lines)


def build_anchors(texts: list[str], tracked: list[str], allowed_roots=None) -> list[dict]:
    """Fotografía de las referencias del checkpoint que HOY son verdaderas."""
    paths, symbols = extract_refs(texts)
    anchors: list[dict] = []
    search_files: list[Path] = []

    for ref, raw, line in paths:
        p = _resolve(raw, tracked)
        if p is None or not _usable(p, allowed_roots):
            continue
        anchor = {"ref": ref, "path": str(p)}
        if line:
            lines = _read_lines(p)
            if lines and line <= len(lines) and lines[line - 1].strip():
                anchor["line"] = line
                anchor["line_text"] = _norm_line(lines[line - 1])
        anchors.append(anchor)
        search_files.append(p)

    for t in tracked:
        p = Path(t)
        if p.is_file() and _usable(p, allowed_roots):
            search_files.append(p)
    search_files = list(dict.fromkeys(search_files))

    for symbol in symbols:
        hits = [str(f) for f in search_files if _contains_symbol(f, symbol)]
        if hits:
            anchors.append({"symbol": symbol, "in": hits[:5]})

    return anchors[:MAX_ANCHORS]


def _summarize(issues: list[dict], checked: int, **extra) -> dict:
    out = {"checked": checked, "issues": issues[:MAX_ISSUES], **extra}
    if len(issues) > MAX_ISSUES:
        out["more_issues"] = len(issues) - MAX_ISSUES
    return out


def verify_anchors(anchors: list[dict]) -> dict:
    """Contrasta las anclas guardadas contra el disco actual."""
    issues: list[dict] = []
    for a in anchors:
        if "symbol" in a:
            if not any(_contains_symbol(Path(f), a["symbol"]) for f in a["in"] if Path(f).is_file()):
                issues.append({"ref": a["symbol"], "issue": "symbol_missing", "was_in": a["in"][:3]})
            continue

        p = Path(a["path"])
        if not p.is_file():
            issues.append({"ref": a["ref"], "issue": "file_missing"})
            continue
        if not a.get("line"):
            continue
        lines = _read_lines(p) or []
        n, expected = a["line"], a["line_text"]
        if n <= len(lines) and _norm_line(lines[n - 1]) == expected:
            continue
        found = [i + 1 for i, line in enumerate(lines) if _norm_line(line) == expected]
        if found:
            issues.append({"ref": a["ref"], "issue": "line_moved",
                           "now_line": min(found, key=lambda i: abs(i - n))})
        else:
            issues.append({"ref": a["ref"], "issue": "line_changed", "expected": expected[:120]})
    return _summarize(issues, len(anchors))


def verify_legacy(texts: list[str], tracked: list[str]) -> dict:
    """Checkpoints sin anclas: solo rutas absolutas que ya no existen."""
    paths, _ = extract_refs(texts)
    absolute = [(ref, raw) for ref, raw, _ in paths if Path(raw).expanduser().is_absolute()]
    issues = [{"ref": ref, "issue": "file_missing"}
              for ref, raw in absolute if not Path(raw).expanduser().is_file()]
    return _summarize(issues, len(absolute), mode="legacy")
