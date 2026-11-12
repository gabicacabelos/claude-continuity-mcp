# Releasing

The plugin's `.mcp.json` pins the server to an exact commit. That way `uvx` starts from its
cache, without contacting GitHub on every launch, and a push to `main` (even a README edit)
doesn't trigger a reinstall that can hit the client's 30 s connection timeout.

To release a new version:

1. Bump `VERSION` in `server.py` and `version` in `.claude-plugin/plugin.json`, then commit.
2. Copy that commit's full SHA (`git rev-parse HEAD`) into the `git+https://…@<sha>` argument
   of `.mcp.json`, then commit.
3. Push both commits. Users get the new server with
   `claude plugin update project-continuity@project-continuity`.

If a launch fails with "Git operation failed", uv's git cache for the repo is probably
corrupt (an interrupted download). Delete
`%LOCALAPPDATA%\uv\cache\git-v0\db\<hash>` and the matching `checkouts\<hash>` folder (on
macOS/Linux, `~/.cache/uv/git-v0/...`), then launch again.
