---
description: Manage Claude Code session history — soft-delete with 5-day trash retention (all running sessions protected)
argument-hint: (no args)
---

Interactive session cleanup. All formatting and menu logic lives in `~/.claude/commands/lib/clean_sessions.py`.

## Rules

- All assistant-visible text is in **English** (this command is distributed publicly).
- When the helper returns a `"markdown"` string, echo it verbatim in your assistant message. Do not reformat.
- When the helper returns an options array, pass it directly to `AskUserQuestion` (label + description as given). Cancel is implicit via "Other".
- **Never** run any mutating filesystem command other than the documented `clean_sessions.py` subcommands. This includes `rm`, `mv`, `shutil.rmtree`, `python3 -c "..."` with `os`/`shutil`/`pathlib` writes, or similar.
- **Always** terminate argument lists with `--` before file paths when calling `trash` / `restore`, and **shell-quote every path** (e.g. `python3 … trash -- "<path1>" "<path2>"`). This prevents argparse from treating `-`-prefixed filenames as flags and handles spaces / special characters safely.
- If you are tempted to ask "continue?" / "next?" as plain text in an active loop (Branch B, Branch R), STOP and use `AskUserQuestion` instead. The navigation question is the only way to collect the user's next action inside a loop.

## Flow

### Step 0 — silent auto-purge

```bash
python3 ~/.claude/commands/lib/clean_sessions.py purge-expired
```
If `purged` is non-empty, say `"Auto-purged N expired trash folders (X freed)."`. Else silent.

### Step 1 — summary

```bash
python3 ~/.claude/commands/lib/clean_sessions.py render-summary
```
Echo the output.

### Step 2 — main menu

```bash
python3 ~/.claude/commands/lib/clean_sessions.py menu-options --context menu
```
Pass the returned array to `AskUserQuestion` (question: `"What would you like to do?"`, header: `"Action"`).

Route by selected label:

| Label (prefix) | Branch |
|---|---|
| `Delete everything older than 90 days` | Q(90) |
| `Delete everything older than 30 days` | Q(30) |
| `Delete everything older than 7 days` | Q(7) |
| `Browse & pick` | B |
| `Restore from trash` | R |
| `Empty trash permanently` | P |
| `Do nothing` / Other | Stop with `"Cancelled."` |

---

## Branch Q — bulk delete older than N days

1. `list-live --older-than-days N` — get the candidates. Show count + total size. If >10, show first 5 + last 5.
2. Confirm with `AskUserQuestion` (`"Yes, move all N to trash"` / `"No, cancel"`).
3. Run `trash-older-than N`. Report status counts + bytes freed.
4. Go back to Step 1.

---

## Branch B — browse & pick (paginated)

Maintain `page = 1` across iterations.

**Loop:**

```bash
python3 ~/.claude/commands/lib/clean_sessions.py render-live-page --page <page>
```
- Echo `result.markdown` verbatim.
- Remember `result.entries` (index → path map) for the current page.
- Remember `result.total_pages` for navigation state.

If `result.total_candidates == 0`: say `"All done — no deletable sessions left."` and go to Step 1.

```bash
python3 ~/.claude/commands/lib/clean_sessions.py menu-options --context browse --page <page> --total-pages <total_pages>
```
Pass options to `AskUserQuestion` (question: `"What next?"`, header: `"Navigate"`).

Route:

- **Pick from this page** — follow-up free-text `"Enter numbers (e.g. 1,3,5-8):"`. Parse ranges; map to paths using `result.entries`. Confirm count+bytes with yes/no AskUserQuestion. On yes: `trash -- "<path1>" "<path2>" …`. Report. **Loop back** (same page, re-fetch — never ask "continue?" in text).
- **Delete all on this page** — collect all paths from `result.entries`. Confirm count+bytes. `trash -- "<path1>" "<path2>" …`. Report. **Loop back.**
- **Delete all (except protected)** — fetch full live list, collect non-current paths. Confirm total. `trash -- "<path1>" "<path2>" …`. Report. **Loop back** (list likely empty → goes to Step 1).
- **Next page** — `page += 1`, loop.
- **Previous page** — `page -= 1`, loop.
- **Back to menu** / **Other** — break, go to Step 1.

After any delete, if `page > total_pages` after the delete, clamp `page = total_pages`.

---

## Branch R — restore

```bash
python3 ~/.claude/commands/lib/clean_sessions.py render-trash-list
```
Echo `result.markdown`. Remember `result.entries`.

Ask for numbers via follow-up free-text. Map to paths. Run `restore -- "<path1>" "<path2>" …`. Report per-file status (flag any `conflict`). Go back to Step 1.

(If the trash has many items we can paginate later — for now a single list is fine since trash is bounded by the 5-day window.)

---

## Branch P — empty trash

Show trash total bytes (from `stats`). yes/no `AskUserQuestion`. On yes: `purge-all`. Report count + size freed. Go back to Step 1.

---

## Safety (enforced by the script)

- Running sessions detected via `ps`+`lsof`, cannot be trashed
- Script confined to `~/.claude/projects/` and `~/.claude/.session-trash/`
- All operations logged to `~/.claude/session-cleanup.log`
- Restore conflicts refused (not overwritten)
- `trash-older-than` auto-excludes current session
