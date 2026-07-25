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
