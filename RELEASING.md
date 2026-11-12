# Releasing

The server is published on PyPI as `claude-continuity-mcp`. The plugin's `.mcp.json` pins the exact
version (`uvx claude-continuity-mcp==X.Y.Z`), so a launch needs neither git nor GitHub, starts from
uv's cache, and works offline after the first run.

## Steps

1. **Bump the version.** Set `VERSION` in `server.py`, commit, push to `main` and wait for the `tests` workflow to pass.
2. **Tag and push the tag:**
   ```bash
   git tag vX.Y.Z
   ```
   ```bash
   git push origin vX.Y.Z
   ```
   The `publish` workflow checks that the tag matches `VERSION`, runs the tests, builds the wheel,
   smoke-tests it (no git, no repo) and publishes it to PyPI through Trusted Publishing. No token is stored.
3. **Confirm the version is live,** then check it from a clean environment:
   ```bash
   python ~/.claude/skills/launch-premortem/scripts/fresh_env.py --no-git --offline-second-run -- uvx claude-continuity-mcp==X.Y.Z
   ```
   Check https://pypi.org/project/claude-continuity-mcp/ for the version.
4. **Only after that,** point the plugin at it. Set `.mcp.json` to `uvx claude-continuity-mcp==X.Y.Z`,
   set `version` in `.claude-plugin/plugin.json` to `X.Y.Z`, commit and push. Users get it with
   `claude plugin update project-continuity@project-continuity`.
5. **If install instructions changed,** rebuild the Agensi zip from `skills/project-continuity/SKILL.md`
   and re-upload it. That triggers a 24–48 h review.

Never point `.mcp.json` at a version that isn't on PyPI yet: every plugin user would fail to launch.

## Troubleshooting a broken launch

- **`Git operation failed`:** an old git-based install with a corrupt uv git cache. Switch the user to the PyPI command,
  or delete `%LOCALAPPDATA%\uv\cache\git-v0\{db,checkouts}\<hash>` (`~/.cache/uv/...` on macOS/Linux).
- **Stale version:** `uv cache clean claude-continuity-mcp`.
