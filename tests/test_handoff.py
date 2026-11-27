"""
Tests del handoff y el autoguardado: extraer el estado de la transcripción, leer git,
armar el handoff y guardarlo. Los helpers de arriba los reusan todas las pruebas.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from router import compaction as C
from router import gitstate as G

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _line(kind, content, **extra):
    """Una línea JSONL de transcripción; `extra` agrega campos de nivel superior (p. ej. cwd)."""
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, **extra})


def _transcript(path, prompts, edited=(), cwd=None, last_text=None):
    """
    Escribe una transcripción falsa en `path` y la devuelve: un pedido por línea de usuario,
    un Edit por archivo en `edited` y, si hay `last_text`, un último mensaje de texto del asistente.
    Con `cwd`, todas las líneas lo llevan.
    """
    extra = {"cwd": str(cwd)} if cwd is not None else {}
    lines = [_line("user", p, **extra) for p in prompts]
    lines += [_line("assistant", [{"type": "tool_use", "id": f"e{i}", "name": "Edit",
                                   "input": {"file_path": str(f), "old_string": "a", "new_string": "b"}}], **extra)
              for i, f in enumerate(edited)]
    if last_text is not None:
        lines.append(_line("assistant", [{"type": "text", "text": last_text}], **extra))
    path = Path(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _git(repo, *args):
    """Corre git en `repo` con identidad fija y sin firmar; devuelve stdout sin el salto final."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
    done = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=str(repo), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    return done.stdout.rstrip()


def _init_repo(path):
    """Repo git en `path` con rama `main` y un commit "first" que agrega a.txt."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "first")
    return path


# ─── Tarea 1: extract_session_state devuelve el último mensaje del asistente y el cwd ───

def test_extract_returns_last_assistant_text_and_cwd(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text("\n".join([
        _line("user", "fix the refresh flow", cwd="/w/app"),
        _line("assistant", [{"type": "text", "text": "I will look at auth.py"}]),
        _line("assistant", [{"type": "tool_use", "id": "1", "name": "Read",
                             "input": {"file_path": "/w/app/auth.py"}}]),
        _line("assistant", [{"type": "text", "text": "Next:   add the   refresh test"},
                            {"type": "thinking", "thinking": "secret"}], cwd="/w/app/src"),
        # un turno que solo llama herramientas y no trae cwd no borra lo anterior
        _line("assistant", [{"type": "tool_use", "id": "2", "name": "Read",
                             "input": {"file_path": "/w/app/src/auth.py"}}]),
    ]) + "\n", encoding="utf-8")
    st = C.extract_session_state(t)
    assert st["last_assistant"] == "Next: add the refresh test"
    assert st["cwd"] == "/w/app/src"
    assert st["prompts"] == ["fix the refresh flow"], "las claves anteriores no cambian"


def test_extract_last_assistant_is_capped_and_empty_without_text(tmp_path):
    long_one = _transcript(tmp_path / "a.jsonl", ["fix it"], last_text="x" * 5000)
    st = C.extract_session_state(long_one)
    assert st["last_assistant"] == "x" * C.MAX_LAST_ASSISTANT_CHARS
    assert C.MAX_LAST_ASSISTANT_CHARS == 600

    no_text = _transcript(tmp_path / "b.jsonl", ["fix it"], edited=["/w/app/a.py"])
    st = C.extract_session_state(no_text)
    assert st["last_assistant"] == "" and st["cwd"] is None
    assert st["edited"] == ["/w/app/a.py"]


# ─── Tarea 2: estado de git de solo lectura y exclusión local ───

@needs_git
def test_git_state_not_a_repo(tmp_path):
    assert G.git_state(tmp_path) == {"available": False}
    assert G.project_root(tmp_path) is None and G.project_root(None) is None
    # una carpeta que no existe tampoco rompe nada
    assert G.git_state(tmp_path / "missing") == {"available": False}
    assert G.project_root(tmp_path / "missing") is None


def test_gitstate_without_git_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # carpeta vacía: no hay ejecutable `git`
    assert G.git_state(tmp_path) == {"available": False}
    assert G.project_root(tmp_path) is None
    assert G.add_local_exclude(tmp_path, ".claude-continuity/") is False


def test_gitstate_on_timeout_never_raises(tmp_path, monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(G.subprocess, "run", hang)
    assert G.git_state(tmp_path) == {"available": False}
    assert G.project_root(tmp_path) is None
    assert G.add_local_exclude(tmp_path, ".claude-continuity/") is False


@needs_git
def test_git_state_dirty_files_and_head(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "b c.txt").write_text("new\n", encoding="utf-8")
    st = G.git_state(repo)
    assert st["available"] is True
    assert Path(st["root"]).resolve() == repo.resolve()
    assert st["branch"] == "main" and st["subject"] == "first" and st["upstream"] is None
    assert _git(repo, "rev-parse", "HEAD").startswith(st["head"])
    assert sorted(st["dirty"]) == ["a.txt", "b c.txt"] and st["dirty_total"] == 2
    assert st["ahead"] == 0 and st["behind"] == 0


@needs_git
def test_git_state_ahead_of_upstream(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), "clone")
    clone = tmp_path / "clone"
    _git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    (clone / "a.txt").write_text("one\n", encoding="utf-8")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-q", "-m", "base")
    _git(clone, "push", "-q", "-u", "origin", "main")
    (clone / "b.txt").write_text("two\n", encoding="utf-8")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-q", "-m", "local work")
    st = G.git_state(clone)
    assert st["upstream"] == "origin/main" and st["ahead"] == 1 and st["behind"] == 0
    assert st["branch"] == "main" and st["subject"] == "local work" and st["dirty"] == []

    # el remoto avanza por otro lado: la rama diverge (ahead 1, behind 1)
    _git(tmp_path, "clone", "-q", str(remote), "other")
    other = tmp_path / "other"
    (other / "c.txt").write_text("three\n", encoding="utf-8")
    _git(other, "add", "c.txt")
    _git(other, "commit", "-q", "-m", "elsewhere")
    _git(other, "push", "-q", "origin", "main")
    _git(clone, "fetch", "-q")
    st = G.git_state(clone)
    assert st["ahead"] == 1 and st["behind"] == 1


@needs_git
def test_git_state_empty_repo_and_detached_head(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    (empty / "x.txt").write_text("x\n", encoding="utf-8")
    st = G.git_state(empty)
    assert st["available"] is True and st["branch"] == "main"
    assert st["head"] is None and st["subject"] is None
    assert st["dirty"] == ["x.txt"] and st["ahead"] == 0 and st["behind"] == 0

    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-q", "--detach")
    st = G.git_state(repo)
    assert st["available"] is True and st["branch"] is None and st["upstream"] is None
    assert st["head"] and st["subject"] == "first"


@needs_git
def test_git_state_caps_the_dirty_list(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    for i in range(40):
        (repo / f"f{i:02d}.txt").write_text("x\n", encoding="utf-8")
    st = G.git_state(repo)
    assert len(st["dirty"]) == 30 and st["dirty_total"] == 40
    assert len(G.git_state(repo, max_files=5)["dirty"]) == 5


@needs_git
def test_git_state_collapses_untracked_dirs_and_skips_rename_sources(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "newdir").mkdir()
    for name in ("one.txt", "two.txt", "three.txt"):
        (repo / "newdir" / name).write_text("x\n", encoding="utf-8")
    _git(repo, "mv", "a.txt", "renamed.txt")
    st = G.git_state(repo)
    # la carpeta nueva cuenta como una entrada y el renombrado como una sola (sin la ruta de origen)
    assert sorted(st["dirty"]) == ["newdir/", "renamed.txt"] and st["dirty_total"] == 2


@needs_git
def test_project_root_from_a_subdirectory(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "src" / "api").mkdir(parents=True)
    assert G.project_root(repo / "src" / "api").resolve() == repo.resolve()
    assert G.project_root(str(repo)).resolve() == repo.resolve()
    st = G.git_state(repo / "src" / "api")
    assert Path(st["root"]).resolve() == repo.resolve()


@needs_git
def test_add_local_exclude_is_idempotent_and_hides_the_folder(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert G.add_local_exclude(repo, ".claude-continuity/") and G.add_local_exclude(repo, ".claude-continuity/")
    (repo / ".claude-continuity").mkdir()
    (repo / ".claude-continuity" / "HANDOFF.md").write_text("x", encoding="utf-8")
    assert _git(repo, "status", "--porcelain") == ""
    lines = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert lines.count(".claude-continuity/") == 1
    assert G.add_local_exclude(tmp_path / "no-repo", ".claude-continuity/") is False


@needs_git
def test_add_local_exclude_creates_info_dir_and_keeps_existing_lines(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    info = repo / ".git" / "info"
    shutil.rmtree(info)
    assert G.add_local_exclude(repo, ".claude-continuity/")
    assert (info / "exclude").read_text(encoding="utf-8") == ".claude-continuity/\n"

    # un archivo existente sin salto de línea final no pega la entrada nueva a la última línea
    (info / "exclude").write_text("*.log", encoding="utf-8")
    assert G.add_local_exclude(repo, ".claude-continuity/")
    assert (info / "exclude").read_text(encoding="utf-8") == "*.log\n.claude-continuity/\n"
