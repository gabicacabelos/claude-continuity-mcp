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
from router import transcripts as T

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


# ─── Tarea 3: ubicar la transcripción de Claude Code de un proyecto ───

def _session(projects, folder, name, mtime, **kw):
    """Crea `projects/<folder>/<name>.jsonl` (vía `_transcript`) con el mtime dado y devuelve su ruta."""
    d = Path(projects) / folder
    d.mkdir(parents=True, exist_ok=True)
    f = _transcript(d / f"{name}.jsonl", ["hi"], **kw)
    os.utime(f, (mtime, mtime))
    return f


def test_encode_project_dir_matches_claude_code_layout():
    assert T.encode_project_dir("/home/user/claude-continuity-mcp") == "-home-user-claude-continuity-mcp"
    assert T.encode_project_dir("C:\\work\\shop-app") == "C--work-shop-app"
    assert T.encode_project_dir(Path("/a/b.c_d")) == "-a-b-c-d"


def test_find_transcript_picks_newest_session_or_the_requested_one(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    folder = T.encode_project_dir(proj)
    older = _session(projects, folder, "old", 1_000_000)
    newer = _session(projects, folder, "new", 2_000_000)
    # un .jsonl dentro de subagents/ es más nuevo que ambos y no cuenta: solo el nivel superior
    sub = projects / folder / "subagents"
    sub.mkdir()
    deeper = _transcript(sub / "agent-1.jsonl", ["sub"])
    os.utime(deeper, (3_000_000, 3_000_000))
    (projects / folder / "notes.txt").write_text("x", encoding="utf-8")
    os.utime(projects / folder / "notes.txt", (4_000_000, 4_000_000))

    assert T.find_transcript(proj, projects_dir=projects) == newer
    assert T.find_transcript(str(proj), session_id="old", projects_dir=str(projects)) == older
    assert T.find_transcript(proj, session_id="nope", projects_dir=projects) is None


def test_find_transcript_falls_back_to_scanning_cwd(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    match = _session(projects, "unrelated-name", "mine", 2_000_000, cwd=proj)
    _session(projects, "another-folder", "theirs", 3_000_000, cwd=tmp_path / "elsewhere")
    assert T.find_transcript(proj, projects_dir=projects) == match
    assert T.find_transcript(tmp_path / "other", projects_dir=projects) is None


def test_find_transcript_fallback_looks_past_the_newest_file_of_a_folder(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    mine = _session(projects, "weird", "mine", 1_000_000, cwd=proj)
    # la más nueva de la carpeta es de otro proyecto: no alcanza con mirar solo esa
    _session(projects, "weird", "other", 2_000_000, cwd=tmp_path / "elsewhere")
    assert T.find_transcript(proj, projects_dir=projects) == mine


def test_find_transcript_fallback_takes_the_newest_match_across_folders(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    _session(projects, "f1", "a", 1_000_000, cwd=proj)
    newest = _session(projects, "f2", "b", 3_000_000, cwd=proj)
    _session(projects, "f3", "c", 2_000_000, cwd=proj)
    assert T.find_transcript(proj, projects_dir=projects) == newest


def test_find_transcript_fallback_reads_only_the_first_50_lines_and_skips_junk(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    d = projects / "odd"
    d.mkdir(parents=True)
    junk = ["not json at all", "[1, 2]", '"str"', "42", "null", '{"cwd": 7}', '{"cwd": ""}', "{broken", ""]
    # cwd ajeno en una línea antes: sigue leyendo hasta la que coincide (línea 12, dentro de las 50)
    inside = d / "inside.jsonl"
    inside.write_text("\n".join(junk + ['{"cwd": "/elsewhere"}', '{"type": "user"}', json.dumps({"cwd": str(proj)})]) + "\n",
                      encoding="utf-8")
    assert T.find_transcript(proj, projects_dir=projects) == inside
    inside.unlink()
    # el cwd aparece recién en la línea 51: queda fuera de la ventana
    late = d / "late.jsonl"
    late.write_text("\n".join(['{"type": "user"}'] * 50 + [json.dumps({"cwd": str(proj)})]) + "\n", encoding="utf-8")
    assert T.find_transcript(proj, projects_dir=projects) is None
    # bytes que no son UTF-8 no rompen la lectura
    late.write_bytes(b"\xff\xfe\x00 garbage\n" + json.dumps({"cwd": str(proj)}).encode("utf-8") + b"\n")
    assert T.find_transcript(proj, projects_dir=projects) == late


def test_find_transcript_fallback_examines_at_most_the_20_newest_files_per_folder(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    mine = _session(projects, "busy", "mine", 1_000, cwd=proj)  # la más vieja de 21: queda fuera
    for i in range(20):
        _session(projects, "busy", f"s{i}", 10_000 + i, cwd=tmp_path / "elsewhere")
    assert T.find_transcript(proj, projects_dir=projects) is None
    os.utime(mine, (99_000, 99_000))  # ahora está entre las 20 más nuevas
    assert T.find_transcript(proj, projects_dir=projects) == mine


def test_find_transcript_fallback_compares_paths_normalized(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    match = _session(projects, "odd", "s", 1_000_000, cwd=str(proj) + os.sep + "sub" + os.sep + "..")
    assert T.find_transcript(proj, projects_dir=projects) == match
    assert T.find_transcript(str(proj) + os.sep, projects_dir=projects) == match


def test_find_transcript_fallback_with_session_id_only_considers_that_file(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    wanted = _session(projects, "odd", "wanted", 1_000_000, cwd=proj)
    _session(projects, "odd", "newer", 2_000_000, cwd=proj)
    _session(projects, "odd2", "wanted", 3_000_000, cwd=tmp_path / "elsewhere")  # mismo nombre, otro proyecto
    assert T.find_transcript(proj, session_id="wanted", projects_dir=projects) == wanted
    assert T.find_transcript(proj, session_id="missing", projects_dir=projects) is None


def test_find_transcript_rejects_session_ids_that_escape_the_folder(tmp_path):
    proj, projects = tmp_path / "proj", tmp_path / "projects"
    _session(projects, T.encode_project_dir(proj), "real", 1_000_000)
    _transcript(projects / "outside.jsonl", ["x"], cwd=proj)  # lo alcanzaría "../outside" si no se filtrara
    for bad in ("../outside", "sub/real", "", ".", os.sep + "abs"):
        assert T.find_transcript(proj, session_id=bad, projects_dir=projects) is None


def test_find_transcript_never_raises_on_odd_input(tmp_path):
    assert T.find_transcript(tmp_path / "proj", projects_dir=tmp_path / "does-not-exist") is None
    assert T.find_transcript("", projects_dir=tmp_path) is None
    assert T.find_transcript(tmp_path / "proj", projects_dir=tmp_path / "file.txt") is None
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert T.find_transcript(tmp_path / "proj", projects_dir=tmp_path / "file.txt") is None
    # una carpeta llamada *.jsonl no es una transcripción
    projects = tmp_path / "projects"
    (projects / T.encode_project_dir(tmp_path / "proj") / "dir.jsonl").mkdir(parents=True)
    assert T.find_transcript(tmp_path / "proj", projects_dir=projects) is None


def test_claude_projects_dir_honors_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert T.claude_projects_dir() == tmp_path / "projects"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert T.claude_projects_dir() == Path.home() / ".claude" / "projects"
    # vacía o solo espacios cuenta como no definida
    for blank in ("", "   "):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", blank)
        assert T.claude_projects_dir() == Path.home() / ".claude" / "projects"


def test_find_transcript_defaults_to_claude_projects_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    proj = tmp_path / "proj"
    f = _session(tmp_path / "cfg" / "projects", T.encode_project_dir(proj), "s", 1_000_000)
    assert T.find_transcript(proj) == f
