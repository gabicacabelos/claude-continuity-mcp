"""
claude-continuity-handoff: arma el handoff de una sesión de Claude Code a pedido, para retomarla en otra
cuenta, otra máquina u otro agente. Es la cara manual del autoguardado (hooks/autosave.py): usa las mismas
piezas de `router/handoff.py` (todo sale de la transcripción y de git, sin resúmenes de un LLM, y las
credenciales salen enmascaradas), con `stopped_by="manual"`.

Sin --write no escribe nada. --commit commitea SOLO HANDOFF.md (`git commit --only`) y --push sube la rama.
La salida pedida va a stdout y los avisos a stderr. Códigos: 0 = ok · 1 = falló el commit o el push (la
salida se imprime igual) · 2 = uso incorrecto o sin transcripción local (la sesión pudo correr en la nube) ·
3 = no se pudo guardar el checkpoint o el archivo (la salida se imprime igual y no se commitea nada).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import handoff
from .gitstate import project_root
from .paths import resolve_data_dir
from .transcripts import find_transcript

# Texto de --help: lo ve el usuario, así que va en inglés (el docstring del módulo es para quien lee el código).
HELP = """
Build a handoff of a Claude Code session, to pick it up in another account, machine or agent. Everything is
read from the local transcript and git (no LLM summaries), and secrets pasted into the session are masked.

  claude-continuity-handoff                  # text to paste (up to 3000 characters) for the newest session
  claude-continuity-handoff --session ID     # another session of the same project
  claude-continuity-handoff --format md      # the full markdown (what HANDOFF.md holds)
  claude-continuity-handoff --format json    # the handoff as JSON
  claude-continuity-handoff --write          # also save the checkpoint and .claude-continuity/HANDOFF.md
  claude-continuity-handoff --commit         # --write, then commit ONLY that file
  claude-continuity-handoff --push           # --commit, then `git push -u origin HEAD`

The output goes to stdout; notices go to stderr. Nothing is written without --write. HANDOFF.md holds your
recent requests (secrets are masked, other private text is not): read it before pushing it anywhere.
Exit codes: 0 ok; 1 the commit or push failed (the output is still printed); 2 bad usage or no local
transcript (the session may have run in the cloud: then the handoff is the HANDOFF.md pushed to the branch);
3 the checkpoint or HANDOFF.md could not be saved (the output is still printed and nothing is committed).
"""

COMMIT_MESSAGE = "chore: handoff from Claude Code"
GIT_TIMEOUT = 60  # segundos por paso: un push lento no debe colgar el comando para siempre
MIN_PASTE_CHARS = 200  # desde acá render_paste garantiza el tope

NO_TRANSCRIPT = ("{what}\n"
                 "The session may have run in the cloud (Claude Code on the web), so there is nothing local to read. "
                 "In that case the handoff is whatever HANDOFF.md was pushed to the branch: fetch the branch and "
                 "open .claude-continuity/HANDOFF.md, or ask the cloud session to write and push it.")
COMMIT_WARNING = ("Note: HANDOFF.md contains your recent requests to Claude (secrets are masked, other private "
                  "text is not). Read it before pushing it anywhere other people can see.")


def _path(value: str) -> Path:
    """Ruta de un flag: cmd.exe y otras capas de comillas pueden entregarla con las comillas puestas."""
    return Path(value.strip().strip("\"'")).expanduser()


def _find_session_transcript(project: Path, session_id: str | None) -> Path | None:
    """
    La transcripción local más nueva del proyecto (o la de `session_id`). Claude Code nombra la carpeta con el
    directorio donde arrancó la sesión, que puede ser la raíz del repo y no la subcarpeta donde corremos:
    si ahí no hay nada se prueba también con la raíz del repo.
    """
    found = find_transcript(project, session_id=session_id)
    if found is None:
        root = project_root(project)
        if root is not None and os.path.normcase(os.path.normpath(root)) != os.path.normcase(str(project)):
            found = find_transcript(root, session_id=session_id)
    return found


def _git_message(done: subprocess.CompletedProcess) -> str:
    """El motivo de un git que falló: git escribe los errores a stderr, pero "nothing to commit" a stdout."""
    return (done.stderr or "").strip() or (done.stdout or "").strip() or f"git exited with code {done.returncode}"


def commit_handoff(root: str | Path, file_path: str | Path, push: bool) -> dict:
    """
    Commitea solo `file_path` (lo que otro tenga en el índice no entra) y, con `push`, sube la rama
    actual con `git push -u origin HEAD`. `git add -f` porque la carpeta del handoff está en el ignore
    local del repo. Si el archivo ya está commiteado tal cual (reintentar un push que falló) no hace un
    commit vacío y sigue con el push. Nunca lanza: devuelve {"committed", "pushed", "error"}, y ante el
    primer paso que falla corta y deja en `error` lo que dijo git (o la excepción).
    """
    result = {"committed": False, "pushed": False, "error": None}

    def git(*args):
        return subprocess.run(["git", *args], cwd=str(root), shell=False, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT)

    try:
        rel = Path(os.path.relpath(file_path, root)).as_posix()
        done = git("add", "-f", "--", rel)
        if done.returncode == 0:
            # 0 = el archivo ya está commiteado igual; 1 = hay cambios que commitear; otro = falló
            changes = git("diff", "--cached", "--quiet", "--", rel)
            if changes.returncode not in (0, 1):
                done = changes
            elif changes.returncode == 1:
                done = git("commit", "--only", "-m", COMMIT_MESSAGE, "--", rel)
                result["committed"] = done.returncode == 0
        if done.returncode == 0 and push:
            done = git("push", "-u", "origin", "HEAD")
            result["pushed"] = done.returncode == 0
        if done.returncode != 0:
            result["error"] = _git_message(done)
    except (OSError, subprocess.SubprocessError, ValueError) as e:  # sin git, timeout, ruta imposible
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _render(h: dict, fmt: str, max_chars: int) -> str:
    """El handoff en el formato pedido, terminado en un salto de línea. Para pegar, el salto cuenta dentro del tope."""
    if fmt == "json":
        return json.dumps(h, ensure_ascii=False, indent=2) + "\n"
    if fmt == "md":
        return handoff.render_markdown(h)
    text = handoff.render_paste(h, max_chars=max_chars)
    return text + "\n" if len(text) < max_chars else text


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):  # consolas/pipes de Windows en cp1252
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="claude-continuity-handoff", description=HELP,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="project folder (default: the current directory)")
    ap.add_argument("--session", help="session id (default: the newest session of the project)")
    ap.add_argument("--transcript", help="path to the transcript .jsonl; skips the lookup")
    ap.add_argument("--format", choices=("paste", "md", "json"), default="paste",
                    help="paste = short text for a chat (default); md = the content of HANDOFF.md; json")
    ap.add_argument("--max-chars", type=int, default=handoff.MAX_PASTE_CHARS,
                    help=f"maximum length of the paste text (default {handoff.MAX_PASTE_CHARS}, minimum "
                         f"{MIN_PASTE_CHARS}); --format paste only")
    ap.add_argument("--write", action="store_true",
                    help="save the autosave-<session> checkpoint and write .claude-continuity/HANDOFF.md")
    ap.add_argument("--commit", action="store_true", help="implies --write; commit only HANDOFF.md")
    ap.add_argument("--push", action="store_true",
                    help="implies --commit; push the branch with `git push -u origin HEAD`")
    ap.add_argument("--home", help="data folder of the MCP (like CLAUDE_CONTINUITY_HOME)")
    a = ap.parse_args(argv)
    if a.format == "paste" and a.max_chars < MIN_PASTE_CHARS:
        print(f"--max-chars must be at least {MIN_PASTE_CHARS}: a shorter text cannot hold the handoff.",
              file=sys.stderr)
        return 2
    a.commit = a.commit or a.push
    a.write = a.write or a.commit

    project = Path(os.path.abspath(_path(a.project))) if a.project else Path(os.getcwd())
    if a.transcript:
        transcript = _path(a.transcript)
        if not transcript.is_file():
            print(f"No transcript file at {transcript}.", file=sys.stderr)
            return 2
    else:
        transcript = _find_session_transcript(project, a.session)
        if transcript is None:
            what = (f"No local Claude Code transcript found for session {a.session} in {project}." if a.session
                    else f"No local Claude Code transcript found for {project}.")
            print(NO_TRANSCRIPT.format(what=what), file=sys.stderr)
            return 2
    session_id = a.session or transcript.stem

    h = file = None
    save_failed = False
    if a.write:
        data_dir = _path(a.home) if a.home else resolve_data_dir(Path(__file__).resolve().parent.parent)
        try:
            out = handoff.capture(transcript, project, session_id, "manual", data_dir, write_file=True)
        except OSError as e:  # --home que no es una carpeta, proyecto de solo lectura, disco lleno…
            print(f"Could not save the handoff: {e}", file=sys.stderr)
            save_failed = True
        else:
            h, file = out["handoff"], out["file"]
            print(f"Saved checkpoint {out['checkpoint']} and wrote {file}", file=sys.stderr)
    if h is None:  # sin --write, o no se pudo guardar: el texto pedido sale igual
        h = handoff.collect_handoff(transcript, project, session_id, "manual")
    sys.stdout.write(_render(h, a.format, a.max_chars))
    sys.stdout.flush()

    if save_failed:
        return 3  # sin archivo guardado no hay nada que commitear
    if not a.commit:
        return 0
    print(COMMIT_WARNING, file=sys.stderr)
    done = commit_handoff(h["project_dir"], file, a.push)
    if done["committed"]:
        print(f"Committed {handoff.HANDOFF_DIR}/{handoff.HANDOFF_FILE}", file=sys.stderr)
    if done["pushed"]:
        print("Pushed the current branch to origin", file=sys.stderr)
    if done["error"]:
        print(f"git failed: {done['error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
