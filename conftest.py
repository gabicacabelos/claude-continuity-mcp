import os
import pathlib
import sys
import tempfile

# Garantiza que el paquete `router` sea importable al correr pytest desde
# cualquier directorio (la raíz del repo debe estar en sys.path).
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Los tests que importan server.py no deben tocar la memoria real del usuario.
os.environ["CLAUDE_CONTINUITY_HOME"] = tempfile.mkdtemp(prefix="continuity-test-")
