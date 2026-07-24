#!/usr/bin/env python3
"""
claude-continuity-mcp — Instalador automático

Qué hace:
  1. Instala dependencias Python
  2. Configura claude_desktop_config.json automáticamente
  3. Verifica que server.py compila correctamente

100% local: no crea .env ni pide API keys — el núcleo no usa ninguna.

Uso:
  python install.py
"""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

BANNER = """
╔══════════════════════════════════════════════╗
║   claude-continuity-mcp — Instalador          ║
║   Memoria persistente y compartida (local)    ║
╚══════════════════════════════════════════════╝
"""

PROJECT_DIR = Path(__file__).parent.resolve()
SERVER_SCRIPT = PROJECT_DIR / "server.py"


def print_step(step: str) -> None:
    print(f"\n→ {step}")

def ok(msg: str) -> None:
    print(f"   ✓ {msg}")

def err(msg: str) -> None:
    print(f"   ✗ {msg}")


def get_claude_config_path() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "Claude" / "claude_desktop_config.json"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def install_dependencies() -> bool:
    print_step("Instalando dependencias Python...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(PROJECT_DIR / "requirements.txt"), "--quiet"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok("Dependencias instaladas")
        return True
    else:
        err(f"Error:\n{result.stderr[:400]}")
        return False


def configure_claude_desktop() -> bool:
    config_path = get_claude_config_path()
    print_step(f"Configurando Claude Desktop...")
    print(f"   Ruta: {config_path}")

    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    # En Windows usar forward slashes para el path
    server_path = str(SERVER_SCRIPT).replace("\\", "/")

    # 100% local desde v3.0.0 — sin API keys, sin .env
    config["mcpServers"]["claude-continuity"] = {
        "command": sys.executable,
        "args": [server_path],
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    ok('Servidor "claude-continuity" agregado a Claude Desktop')
    return True


def verify_syntax() -> bool:
    print_step("Verificando sintaxis del servidor...")
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SERVER_SCRIPT)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok("server.py — sintaxis OK")
        return True
    else:
        err(f"Error de sintaxis:\n{result.stderr}")
        return False


def main():
    print(BANNER)
    print(f"Directorio del proyecto: {PROJECT_DIR}")

    steps = [
        ("Instalar dependencias", install_dependencies),
        ("Configurar Claude Desktop", configure_claude_desktop),
        ("Verificar sintaxis", verify_syntax),
    ]

    success_count = 0
    for name, func in steps:
        try:
            if func():
                success_count += 1
        except Exception as e:
            err(f"Error en '{name}': {e}")

    print("\n" + "─" * 48)
    print(f"Resultado: {success_count}/{len(steps)} pasos OK")

    if success_count == len(steps):
        print("""
✓ Instalación completa

Próximos pasos:
  1. Reiniciá Claude Desktop completamente
  2. Verificá escribiendo en Claude: router_status()

Opcional (ranking semántico local para smart_read): pip install fastembed
""")
    else:
        print("\n⚠  Instalación parcial — revisar errores arriba\n")


if __name__ == "__main__":
    main()
