# claude-clean-sessions

A Claude Code slash command for managing your session history with a **soft-delete trash**, **5-day recovery window**, **age-bucket triage**, and **paginated browsing**. All running Claude Code sessions are auto-detected and protected from accidental deletion.

## Why

Claude Code stores every session transcript as a `.jsonl` file under `~/.claude/projects/<encoded-cwd>/`. These accumulate forever — there is no built-in cleanup. Once you have hundreds of sessions, scrolling through them all just to delete a few is painful. This command gives you a safe, interactive, token-efficient way to prune them.

## Features

- **Age-bucket triage** — summary groups sessions by `This week / Recent / Old / Archive` so you can act on the oldest ones with one click
- **One-click bulk delete** — "Delete everything older than 30 days" removes a whole bucket after confirmation
- **Paginated browse** — 20 sessions per page, oldest first; supports `Pick`, `Delete all on this page`, `Delete all (except protected)`, `Next/Previous page`
- **Soft delete** — trashed sessions land in `~/.claude/.session-trash/<YYYY-MM-DD>/`, recoverable for 5 days
- **Auto-purge** — trash older than 5 days is silently removed on each run
- **Multi-session safe** — detects *all* currently-running Claude Code processes via `ps` + `lsof` and refuses to trash their active sessions (not just the invoker's session)
- **Restore flow** — pick by number from the trash list; refuses to overwrite conflicts
- **Audit log** — every move/delete/restore appended to `~/.claude/session-cleanup.log`
- **Token-efficient** — Python renders all lists and menus directly; Claude only echoes pre-formatted markdown and invokes `AskUserQuestion`. ~65% less token usage than a naive JSON-in-Claude-formats-out design.

## Install

```bash
git clone https://github.com/<your-user>/claude-clean-sessions.git
cd claude-clean-sessions
./install.sh
```

The installer copies:
- `commands/clean-sessions.md` → `~/.claude/commands/clean-sessions.md`
- `commands/lib/clean_sessions.py` → `~/.claude/commands/lib/clean_sessions.py` (made executable)

Nothing else is written. Uninstall by removing those two files.

## Usage

In any Claude Code CLI session:

```
/clean-sessions
```

You'll see a summary:

```
Total: 150 sessions, 180M
  This week (0-7d)        25 files, 12M
  Recent (7-30d)          80 files, 45M
  Old (30-90d)            40 files, 90M
  Archive (90d+)           5 files, 33M
  Protected (running):     1
  Trash:                   empty
```

Followed by a dynamic menu (options only appear when applicable):

- **Delete everything older than 90 days (archive)**
- **Delete everything older than 30 days**
- **Delete everything older than 7 days**
- **Browse & pick sessions to delete…**
- **Restore from trash…**
- **Empty trash permanently**
- **Do nothing — close**

Bulk deletes always preview the candidates and require confirmation. Browse paginates 20 per page, with `Pick` / `Delete this page` / `Delete all (except protected)` / `Next` / `Previous` as applicable.

## Requirements

- macOS or Linux (uses `lsof` and `ps`)
- Python 3.9+
- Claude Code CLI

## How protection works

On each run, the helper:

1. Finds all running `claude` processes via `ps -eo pid,command` (matches the Node binary even when it reports its version string as the command name)
2. For each PID, reads its current working directory via `lsof -p <pid> -d cwd`
3. Maps each cwd to the corresponding `~/.claude/projects/<encoded-cwd>/` folder
4. Marks the most recently-modified `.jsonl` in each as protected

This means if you have Claude Code running in three different terminals on three different projects, **all three** active sessions are protected — not just the one you invoked `/clean-sessions` from.

## Architecture

- `commands/clean-sessions.md` — the slash command prompt. Thin; just tells Claude which Python subcommand to run at each step and to echo its markdown verbatim / forward its options to `AskUserQuestion`.
- `commands/lib/clean_sessions.py` — all logic: discovery, filtering, pagination, markdown rendering, menu option generation, trash moves, restore, log writing.

Claude's role is minimal: route, display, and collect user input. This keeps token usage low and behavior deterministic.

## Manual CLI usage

The underlying helper is scriptable. All commands emit JSON or ready-to-render markdown on stdout.

### Data / listing
```bash
python3 ~/.claude/commands/lib/clean_sessions.py stats
python3 ~/.claude/commands/lib/clean_sessions.py bucket-stats
python3 ~/.claude/commands/lib/clean_sessions.py detect-current
python3 ~/.claude/commands/lib/clean_sessions.py list-live [--older-than-days N] [--limit N] [--offset N] [--oldest-first]
python3 ~/.claude/commands/lib/clean_sessions.py list-trash
```

### Pre-rendered output (for the slash command)
```bash
python3 ~/.claude/commands/lib/clean_sessions.py render-summary
python3 ~/.claude/commands/lib/clean_sessions.py render-live-page --page N [--page-size 20]
python3 ~/.claude/commands/lib/clean_sessions.py render-trash-list
python3 ~/.claude/commands/lib/clean_sessions.py menu-options --context menu|browse [--page N --total-pages N]
```

### Mutations
```bash
python3 ~/.claude/commands/lib/clean_sessions.py trash <jsonl-path> [...]
python3 ~/.claude/commands/lib/clean_sessions.py trash-older-than <days>
python3 ~/.claude/commands/lib/clean_sessions.py restore <trash-path> [...]
python3 ~/.claude/commands/lib/clean_sessions.py purge-expired
python3 ~/.claude/commands/lib/clean_sessions.py purge-all
```

## Configuration

Edit `commands/lib/clean_sessions.py`:

- `RETENTION_DAYS = 5` — how long soft-deleted sessions remain recoverable
- `PREVIEW_CHARS = 80` — characters from the first user message shown in listings
- `AGE_BUCKETS` — tuples of `(key, label, lower_days, upper_days)` used for the bucket summary. Tune the buckets if you prefer different breakpoints.

## Safety notes

- Trashing a file moves it with `shutil.move`, preserving mtime and content
- Restore refuses to overwrite an existing live session (reports a `conflict` instead)
- `purge-expired` and `purge-all` use `shutil.rmtree` only on paths verified (via `Path.resolve().relative_to()`) to be inside `~/.claude/.session-trash/`
- The helper never calls `rm -rf` on user input or pattern-matched paths
- `trash-older-than` auto-filters out the protected sessions before calling the underlying `trash` op

## License

MIT. See `LICENSE`.

## Contributing

PRs welcome. Issues especially appreciated for:
- Unusual process names or edge cases in running-session detection
- Path encoding quirks across platforms
- Performance on session collections in the thousands
