# Handoff autosave: manual check

The test suite can't tell these four things, because they depend on Claude Code itself. Run this
once in real sessions, with the plugin build that ships `hooks/autosave.py`. Then update the
"Not verified yet" bullet in the README's "Handoff and autosave" section with what you found.

- [ ] **(a) Does your plan's usage limit fire `StopFailure`?** Add a temporary hook to
  `~/.claude/settings.json` (on Windows replace `cat` with any command that appends stdin to a file):

  ```json
  {"hooks": {"StopFailure": [{"hooks": [{"type": "command", "command": "cat >> ~/stopfailure.log"}]}]}}
  ```

  Work until the plan limit stops a turn, then read `~/stopfailure.log`. To see the payload
  sooner, provoke another API error that fires the same event, for example start `claude` with an
  invalid `ANTHROPIC_API_KEY` and send a message. This may not fire in `-p` mode: an empty log
  there says nothing about the plan limit.
  - Fired with `"error": "rate_limit"`: nothing to change. Remove the temporary hook.
  - Fired with another `error` value: the handoff shows it as written; reword the README where it
    says "usage limit".
  - Never fired (the turn just stops): keep the per-turn autosave and make sure the README says
    to run `claude-continuity-handoff --write` by hand. Don't remove anything.
- [ ] **(b) Is the payload what the hook expects?** In the logged JSON look for `session_id`,
  `transcript_path`, `cwd` and `error`. The hook reads `error`; without `transcript_path` it
  looks for the newest transcript of `cwd` in `~/.claude/projects`.
  - `transcript_path` missing: fine, but confirm `.claude-continuity/HANDOFF.md` still appears.
  - `cwd` missing: the hook uses its own working directory; confirm the file lands in the project.
  - `error` missing: the checkpoint says `api_error`. Acceptable, but note it in the README.
- [ ] **(c) Do the plugin's hooks run in a cloud session?** Start a Claude Code cloud session on a
  repo with the plugin enabled, do one turn, then ask Claude to run
  `ls ~/.claude-continuity/checkpoints/`. Expect an `autosave-<session>.json`.
  - Present: the hooks run in the cloud. Nothing to change.
  - Absent: first confirm the plugin is enabled in that session. If it is, the README's "Local
    and cloud" section must say the cloud autosave doesn't run and that Claude has to run
    `claude-continuity-handoff --push` itself before the session ends.
- [ ] **(d) Windows with accents.** Open a project whose path has accents or an `ñ`
  (`C:\Users\Zoé\proyectos\café`), do one turn, and check that
  `%USERPROFILE%\.claude-continuity\checkpoints\autosave-<session>.json` exists and that its
  `project_dir` shows the accents intact.
  - Missing or garbled: it's an encoding bug in the hook. Open an issue with the path and the
    file; until it's fixed, `claude-continuity-handoff` from a terminal is the fallback.
