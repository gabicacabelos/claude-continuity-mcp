"""
Control de acceso a archivos — 100% local, determinista, sin dependencias.

El modelo elige qué rutas leer. Un prompt injection en cualquier archivo del
proyecto (o una orden del inbox) puede pedir `~/.ssh/id_rsa` o el `.env` de otro
proyecto, y sin control eso se lee Y se persiste en claro en el ledger.

Dos capas, pensadas para no romper instalaciones existentes:

1. DENY-LIST (siempre activa, sin configurar nada): bloquea credenciales y
   material sensible conocido — claves SSH/GPG/nube, .env, *.pem, keystores,
   historiales de shell, perfiles de navegador. Es lo que un atacante busca.

2. ALLOWLIST (opcional, `allowed_roots` en router_config.json): si se define,
   TODO lo que quede fuera de esas raíces se rechaza. Es el modo estricto para
   quien quiera encerrar el MCP en sus proyectos. Vacío = sin restricción de
   raíz (comportamiento por defecto, no rompe nada).
"""

import re
from pathlib import Path

# Directorios que nunca deberían leerse: si alguno de estos aparece como
# componente de la ruta, se rechaza.
SENSITIVE_DIR_PARTS = frozenset({
    ".ssh", ".gnupg", ".gpg", ".aws", ".azure", ".kube", ".docker",
    ".password-store", ".mozilla", ".putty",
    "keychains", "credentials.d",
})

# Nombres exactos (case-insensitive) que no se leen nunca.
SENSITIVE_NAMES = frozenset({
    ".env", ".envrc", ".netrc", "_netrc", ".pgpass", ".my.cnf",
    "credentials", "credentials.json", "client_secret.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".htpasswd", "shadow", "sudoers",
    ".bash_history", ".zsh_history", ".psql_history", ".python_history",
    "secrets.json", "secrets.yaml", "secrets.yml",
})

# Sufijos de material criptográfico / credenciales.
SENSITIVE_SUFFIXES = frozenset({
    ".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx", ".ppk", ".asc",
})

# Prefijos de nombre (ej. ".env.local", ".env.production", "id_rsa.pub").
SENSITIVE_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".env.")


class PathNotAllowed(PermissionError):
    """La ruta pedida está fuera de scope o es material sensible."""


def is_sensitive(path: Path) -> bool:
    """True si la ruta es credencial/material sensible conocido. Barato: solo strings."""
    name = path.name.casefold()
    if name in SENSITIVE_NAMES or name.startswith(SENSITIVE_PREFIXES):
        return True
    if path.suffix.casefold() in SENSITIVE_SUFFIXES:
        return True
    lowered = {part.casefold() for part in path.parts}
    return bool(lowered & SENSITIVE_DIR_PARTS)


def _within(target: Path, root: Path) -> bool:
    try:
        return target == root or target.is_relative_to(root)
    except (ValueError, OSError):
        return False


def check_path(raw_path: str | Path, allowed_roots=None, *, must_exist: bool = True) -> Path:
    """
    Resuelve y valida una ruta. Devuelve la ruta resuelta o lanza PathNotAllowed.

    - Resuelve symlinks y `..` ANTES de validar (si no, `proj/../../.ssh/id_rsa`
      pasaría cualquier chequeo hecho sobre el string crudo).
    - allowed_roots vacío/None → solo aplica la deny-list.
    """
    try:
        p = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        raise PathNotAllowed(f"ruta inválida: {str(e)[:120]}") from e

    if must_exist and not p.exists():
        raise PathNotAllowed(f"no existe: {p}")

    if is_sensitive(p):
        raise PathNotAllowed(
            f"acceso bloqueado a material sensible: {p.name} "
            "(claves, credenciales o secretos — el MCP nunca los lee ni los guarda)"
        )

    roots = [Path(r).expanduser().resolve() for r in (allowed_roots or []) if str(r).strip()]
    if roots and not any(_within(p, r) for r in roots):
        raise PathNotAllowed(
            f"fuera de las raíces permitidas ({', '.join(str(r) for r in roots)}): {p}"
        )
    return p


def is_allowed(raw_path: str | Path, allowed_roots=None) -> bool:
    """Variante booleana para filtrar en bucles (indexado, drain) sin excepciones."""
    try:
        check_path(raw_path, allowed_roots, must_exist=False)
        return True
    except PathNotAllowed:
        return False


# ─── Redacción de secretos por CONTENIDO ──────────────────────────────────────
# La deny-list de arriba bloquea archivos ENTEROS conocidos (.env, *.pem). Esto
# cubre el caso que se le escapa: un secreto real pegado accidentalmente DENTRO
# de un archivo normal (una API key hardcodeada en un config.py, por ejemplo).
# Determinista por shape reconocible — nunca decide "esto parece sensible en
# general", y nunca pasa por un LLM.

_REDACTED = "█REDACTED█"

_SECRET_PATTERNS = (
    # (nombre, regex) — shapes de credencial real, no heurística difusa.
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("openai_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
)

# Asignaciones tipo `password = "..."`. Exige comillas a propósito: en código
# fuente un secreto hardcodeado casi siempre es un string literal, mientras que
# `token = getToken()` o `password = os.environ["PW"]` son referencias, no
# secretos — sin este requisito ambos se enmascaraban como falso positivo.
#
# Sin \b inicial a propósito: `_` es un carácter de palabra en regex, así que
# `DB_PASSWORD`/`MY_SECRET_KEY` (prefijo + guion bajo + palabra clave, un
# patrón de nombrado real y común) no tendrían boundary ahí y se perderían.
# El riesgo de over-matching queda acotado por el resto de la expresión: solo
# dispara con string literal entre comillas de ≥8 chars, no con cualquier cosa.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)
    (\s*[:=]\s*)
    (["'])
    ([^"'\n]{8,})
    \3
    """,
    re.VERBOSE,
)

# Valores de ejemplo/placeholder — no son secretos reales, no enmascarar
# (si no, cualquier .env.example o doc con "api_key=your_key_here" se rompería).
_PLACEHOLDER_VALUES = frozenset({
    "changeme", "change_me", "your_api_key", "your-api-key", "your_key_here",
    "xxxxxxxx", "placeholder", "example", "dummy", "fake", "none", "null",
    "true", "false", "todo", "tbd", "insert_key_here", "replace_me", "secret",
})


def _mask(value: str) -> str:
    if len(value) <= 6:
        return _REDACTED
    return f"{value[:4]}{_REDACTED}"


def redact_secrets(text: str) -> tuple[str, int]:
    """
    Enmascara en el lugar los valores con forma de credencial conocida.
    Devuelve (texto_con_secretos_enmascarados, cantidad_enmascarada).

    Determinista: el mismo valor siempre enmascara igual (mismo prefijo visible
    + mismo marcador), así que un diff entre dos lecturas del mismo secreto sin
    cambios sigue detectando 'unchanged'; si el secreto rota, el diff lo marca
    como cambio real sin exponer ni el valor viejo ni el nuevo.
    """
    count = 0

    def _sub_shape(m: re.Match) -> str:
        nonlocal count
        count += 1
        matched = m.group(0)
        if "\n" in matched:
            # El bloque de clave privada puede abarcar varias líneas: preservar
            # la cantidad de saltos de línea para no correr la numeración del
            # resto del archivo (chunking y diffs dependen de contar líneas).
            return _REDACTED + "\n" * matched.count("\n")
        return _mask(matched)

    for _name, pattern in _SECRET_PATTERNS:
        text = pattern.sub(_sub_shape, text)

    def _sub_assignment(m: re.Match) -> str:
        nonlocal count
        value = m.group(4)
        # Ya enmascarado por un patrón de shape en la pasada anterior (ej. un
        # token de GitHub dentro de `token = "ghp_..."` matchea las dos reglas):
        # no volver a contar ni a enmascarar un valor que ya es el marcador.
        if _REDACTED in value or value.casefold() in _PLACEHOLDER_VALUES:
            return m.group(0)
        count += 1
        quote = m.group(3)
        return f"{m.group(1)}{m.group(2)}{quote}{_mask(value)}{quote}"

    text = _ASSIGNMENT.sub(_sub_assignment, text)
    return text, count
