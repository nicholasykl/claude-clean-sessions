#!/usr/bin/env python3
"""
claude-clean-sessions — interactive session cleanup for Claude Code CLI.

Storage model:
  Live:  ~/.claude/projects/<encoded-cwd>/*.jsonl
  Trash: ~/.claude/.session-trash/<YYYY-MM-DD>/<encoded-cwd>/*.jsonl

Subcommands emit JSON for programmatic use (e.g. by a slash command).

Usage:
  clean_sessions.py list-live [--older-than-days N] [--limit N] [--offset N]
  clean_sessions.py list-trash
  clean_sessions.py detect-current
  clean_sessions.py stats
  clean_sessions.py bucket-stats
  clean_sessions.py trash <session_path> [<session_path> ...]
  clean_sessions.py trash-older-than <days>
  clean_sessions.py restore <trash_path> [<trash_path> ...]
  clean_sessions.py purge-expired
  clean_sessions.py purge-all
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"
TRASH_DIR = CLAUDE_HOME / ".session-trash"
LOG_FILE = CLAUDE_HOME / "session-cleanup.log"
RETENTION_DAYS = 5
PREVIEW_CHARS = 80
LOG_MAX_BYTES = 1_048_576  # 1 MB; rotate after this


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _rotate_log_if_needed() -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            rotated = LOG_FILE.with_suffix(".log.1")
            if rotated.exists():
                rotated.unlink()
            LOG_FILE.rename(rotated)
    except OSError:
        pass


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log_if_needed()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with LOG_FILE.open("a") as f:
        f.write(f"{ts}  {msg}\n")


def decode_project(folder_name: str) -> str:
    """Best-effort reverse of Claude Code's project folder encoding.
    Claude Code replaces `/` with `-` in the absolute cwd path. Since original
    paths may contain `-`, exact reversal is ambiguous; we show a readable form
    by replacing the leading `-` with `/`."""
    if folder_name.startswith("-"):
        return "/" + folder_name[1:].replace("-", "/")
    return folder_name


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _claude_pids() -> list[int]:
    """Find PIDs of running Claude Code CLI processes.
    Match the command line rather than the process name because the Node.js
    binary shows up with its version (e.g. `2.1.107`) on macOS."""
    if not _which("ps"):
        return []
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,command="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_str, cmd = line.split(None, 1)
        except ValueError:
            continue
        # Broad match: cli name, common wrappers, and the node runtime used by
        # the official CLI. Avoid matching the Claude desktop app.
        is_candidate = (
            "/claude" in cmd
            or cmd.startswith("claude")
            or " claude " in cmd
            or "claude-code" in cmd
            or "@anthropic-ai/claude" in cmd
        )
        if not is_candidate:
            continue
        if "Claude.app" in cmd or "chrome-native-host" in cmd:
            continue
        try:
            pids.append(int(pid_str))
        except ValueError:
            continue
    return pids


def _process_open_jsonls_linux(pid: int) -> list[str]:
    fd_dir = Path(f"/proc/{pid}/fd")
    if not fd_dir.is_dir():
        return []
    paths: list[str] = []
    try:
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.endswith(".jsonl") and str(PROJECTS_DIR) in target:
                paths.append(target)
    except OSError:
        return []
    return paths


def _process_cwd(pid: int) -> str | None:
    # /proc on Linux is authoritative and cheap
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        try:
            return os.readlink(proc_cwd)
        except OSError:
            pass
    # lsof on macOS / BSD
    if not _which("lsof"):
        return None
    try:
        out = subprocess.check_output(
            ["lsof", "-p", str(pid), "-a", "-d", "cwd", "-Fn"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def detection_available() -> bool:
    """True iff we can reasonably detect running sessions on this OS."""
    return Path("/proc").is_dir() or _which("lsof") is not None


def detect_current_sessions() -> list[str]:
    """Identify .jsonl files belonging to currently running Claude Code CLI
    processes. Strategy: find each `claude` PID, read its cwd, map to the
    encoded project folder, then pick the most recently modified .jsonl.
    On Linux, also include any `.jsonl` file actually held open via /proc/<pid>/fd.
    This protects all in-flight sessions, not just the one in the invoker's cwd."""
    if not PROJECTS_DIR.exists():
        return []
    protected: set[str] = set()
    for pid in _claude_pids():
        # Direct open-FD check (Linux only — most accurate)
        for p in _process_open_jsonls_linux(pid):
            protected.add(p)
        cwd = _process_cwd(pid)
        if not cwd:
            continue
        proj_folder = PROJECTS_DIR / _encode_cwd(cwd)
        if not proj_folder.is_dir():
            continue
        sessions = sorted(
            proj_folder.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if sessions:
            protected.add(str(sessions[0]))
    return sorted(protected)


def safe_preview(jsonl_path: Path) -> str:
    """Extract a short preview of the first user message. Tolerate corruption."""
    try:
        with jsonl_path.open("r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message") or obj
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                content = part.get("text", "")
                                break
                        else:
                            content = str(content)
                    text = str(content).replace("\n", " ").strip()
                    return text[:PREVIEW_CHARS]
        return "(no user message found)"
    except Exception as e:
        return f"(preview unavailable: {e.__class__.__name__})"


def human_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def list_live(
    older_than_days: int | None = None,
    limit: int | None = None,
    offset: int = 0,
    oldest_first: bool = False,
) -> list[dict]:
    current = set(detect_current_sessions())
    items: list[dict] = []
    if not PROJECTS_DIR.exists():
        return items
    cutoff_ts: float | None = None
    if older_than_days is not None:
        cutoff_ts = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
    for proj in sorted(PROJECTS_DIR.iterdir()):
        if not proj.is_dir() or proj.is_symlink():
            continue
        for f in sorted(proj.glob("*.jsonl")):
            if f.is_symlink():
                continue
            stat = f.stat()
            # "older than N days" means age >= N days, i.e. mtime <= cutoff
            if cutoff_ts is not None and stat.st_mtime > cutoff_ts:
                continue
            items.append({
                "path": str(f),
                "project_encoded": proj.name,
                "project_decoded": decode_project(proj.name),
                "size": stat.st_size,
                "size_human": human_size(stat.st_size),
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "mtime_ts": stat.st_mtime,
                "preview": safe_preview(f),
                "is_current": str(f) in current,
            })
    items.sort(key=lambda x: x["mtime_ts"], reverse=not oldest_first)
    if offset:
        items = items[offset:]
    if limit is not None:
        items = items[:limit]
    return items


AGE_BUCKETS = [
    ("this_week", "This week (0-7d)", 0, 7),
    ("recent", "Recent (7-30d)", 7, 30),
    ("old", "Old (30-90d)", 30, 90),
    ("archive", "Archive (90d+)", 90, None),
]


def bucket_stats() -> dict:
    """Break sessions down by age bucket for a quick triage view."""
    current = set(detect_current_sessions())
    now = datetime.now(timezone.utc).timestamp()
    buckets: dict[str, dict] = {
        key: {"label": label, "count": 0, "bytes": 0}
        for key, label, _, _ in AGE_BUCKETS
    }
    protected_count = 0
    total_count = 0
    total_bytes = 0
    if PROJECTS_DIR.exists():
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                stat = f.stat()
                total_count += 1
                total_bytes += stat.st_size
                if str(f) in current:
                    protected_count += 1
                    continue
                age_days = (now - stat.st_mtime) / 86400
                for key, _label, lo, hi in AGE_BUCKETS:
                    if age_days >= lo and (hi is None or age_days < hi):
                        buckets[key]["count"] += 1
                        buckets[key]["bytes"] += stat.st_size
                        break
    for b in buckets.values():
        b["bytes_human"] = human_size(b["bytes"])
    return {
        "total_count": total_count,
        "total_bytes": total_bytes,
        "total_human": human_size(total_bytes),
        "protected_count": protected_count,
        "buckets": buckets,
    }


def trash_older_than(days: int) -> list[dict]:
    candidates = list_live(older_than_days=days)
    paths = [c["path"] for c in candidates if not c["is_current"]]
    return trash_files(paths)


def list_trash() -> list[dict]:
    items: list[dict] = []
    if not TRASH_DIR.exists():
        return items
    today = _today_utc()
    for date_folder in sorted(TRASH_DIR.iterdir()):
        if not date_folder.is_dir() or date_folder.is_symlink():
            continue
        try:
            trashed_on = datetime.strptime(date_folder.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        expires_in = RETENTION_DAYS - (today - trashed_on).days
        for proj in sorted(date_folder.iterdir()):
            if not proj.is_dir() or proj.is_symlink():
                continue
            for f in sorted(proj.glob("*.jsonl")):
                if f.is_symlink():
                    continue
                stat = f.stat()
                items.append({
                    "path": str(f),
                    "trashed_date": date_folder.name,
                    "expires_in_days": expires_in,
                    "expired": expires_in <= 0,
                    "project_encoded": proj.name,
                    "project_decoded": decode_project(proj.name),
                    "size": stat.st_size,
                    "size_human": human_size(stat.st_size),
                    "preview": safe_preview(f),
                })
    return items


def stats() -> dict:
    live = list_live()
    trashed = list_trash()
    live_bytes = sum(x["size"] for x in live)
    trash_bytes = sum(x["size"] for x in trashed)
    trash_dates = sorted({x["trashed_date"] for x in trashed})
    current = [x for x in live if x["is_current"]]
    return {
        "live_count": len(live),
        "live_bytes": live_bytes,
        "live_human": human_size(live_bytes),
        "current_count": len(current),
        "current_paths": [x["path"] for x in current],
        "trash_count": len(trashed),
        "trash_bytes": trash_bytes,
        "trash_human": human_size(trash_bytes),
        "trash_date_folders": trash_dates,
        "oldest_trash_date": trash_dates[0] if trash_dates else None,
        "retention_days": RETENTION_DAYS,
    }


def _is_safe_component(name: str) -> bool:
    """A project / filename component must be non-empty, not contain a path
    separator or null byte, and not traverse up."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return True


def _path_within(p: Path, root: Path) -> bool:
    try:
        p.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def ensure_safe_trash_path(p: Path) -> None:
    """Refuse to touch anything outside TRASH_DIR."""
    if not _path_within(p, TRASH_DIR):
        raise SystemExit(f"refusing to operate outside trash: {p}")


def ensure_safe_live_path(p: Path) -> None:
    if not _path_within(p, PROJECTS_DIR):
        raise SystemExit(f"refusing to operate outside projects: {p}")


def _require_detection() -> None:
    """trash/restore mutations refuse to run when we can't detect running sessions."""
    if not detection_available():
        raise SystemExit(
            "refuse to mutate: running-session detection is unavailable "
            "(neither /proc nor lsof found). Install lsof or run on Linux."
        )


def trash_files(paths: Iterable[str]) -> list[dict]:
    _require_detection()
    results: list[dict] = []
    for raw in paths:
        today = _today_utc().strftime("%Y-%m-%d")
        src = Path(raw)
        if src.is_symlink():
            results.append({"path": raw, "status": "refused-symlink"})
            log(f"REFUSED trash of symlink: {src}")
            continue
        ensure_safe_live_path(src)
        if not src.exists():
            results.append({"path": raw, "status": "missing"})
            continue
        # Re-snapshot each iteration to minimise TOCTOU against newly-started sessions.
        current = set(detect_current_sessions())
        if str(src) in current or str(src.resolve()) in current:
            results.append({"path": raw, "status": "refused-current-session"})
            log(f"REFUSED trash of current session: {src}")
            continue
        proj_folder = src.parent.name
        if not _is_safe_component(proj_folder) or not _is_safe_component(src.name):
            results.append({"path": raw, "status": "refused-unsafe-name"})
            log(f"REFUSED trash of unsafe name: {src}")
            continue
        dest_dir = TRASH_DIR / today / proj_folder
        if not _path_within(dest_dir, TRASH_DIR):
            results.append({"path": raw, "status": "refused-unsafe-dest"})
            log(f"REFUSED trash of unsafe dest: {dest_dir}")
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if not _path_within(dest, TRASH_DIR):
            results.append({"path": raw, "status": "refused-unsafe-dest"})
            continue
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            i = 1
            while dest.exists():
                dest = dest_dir / f"{stem}.conflict{i}{suffix}"
                i += 1
        try:
            shutil.move(str(src), str(dest))
            results.append({"path": raw, "status": "trashed", "dest": str(dest)})
            log(f"TRASHED {src} -> {dest}")
        except Exception as e:
            results.append({"path": raw, "status": "error", "error": str(e)})
            log(f"ERROR trashing {src}: {e}")
    return results


def restore_files(paths: Iterable[str]) -> list[dict]:
    results: list[dict] = []
    for raw in paths:
        src = Path(raw)
        if src.is_symlink():
            results.append({"path": raw, "status": "refused-symlink"})
            log(f"REFUSED restore of symlink: {src}")
            continue
        ensure_safe_trash_path(src)
        if not src.exists():
            results.append({"path": raw, "status": "missing"})
            continue
        proj_folder = src.parent.name
        if not _is_safe_component(proj_folder) or not _is_safe_component(src.name):
            results.append({"path": raw, "status": "refused-unsafe-name"})
            log(f"REFUSED restore of unsafe name: {src}")
            continue
        dest_dir = PROJECTS_DIR / proj_folder
        if not _path_within(dest_dir, PROJECTS_DIR):
            results.append({"path": raw, "status": "refused-unsafe-dest"})
            log(f"REFUSED restore of unsafe dest: {dest_dir}")
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if not _path_within(dest, PROJECTS_DIR):
            results.append({"path": raw, "status": "refused-unsafe-dest"})
            continue
        if dest.exists():
            results.append({
                "path": raw,
                "status": "conflict",
                "existing": str(dest),
            })
            log(f"CONFLICT restore {src} -> {dest} (target exists)")
            continue
        try:
            shutil.move(str(src), str(dest))
            results.append({"path": raw, "status": "restored", "dest": str(dest)})
            log(f"RESTORED {src} -> {dest}")
        except Exception as e:
            results.append({"path": raw, "status": "error", "error": str(e)})
            log(f"ERROR restoring {src}: {e}")
    return results


def _safe_rmtree_in_trash(target: Path) -> None:
    """rmtree only when target is confirmed inside TRASH_DIR (not a symlink)."""
    if target.is_symlink():
        raise SystemExit(f"refuse to rmtree symlink: {target}")
    if not _path_within(target, TRASH_DIR):
        raise SystemExit(f"refuse to rmtree outside trash: {target}")
    shutil.rmtree(target)


def purge_expired() -> dict:
    if not TRASH_DIR.exists():
        return {"purged": [], "bytes_freed": 0}
    today = _today_utc()
    purged: list[str] = []
    bytes_freed = 0
    for date_folder in list(TRASH_DIR.iterdir()):
        if not date_folder.is_dir() or date_folder.is_symlink():
            continue
        try:
            d = datetime.strptime(date_folder.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        age_days = (today - d).days
        if age_days > RETENTION_DAYS:
            size = sum(p.stat().st_size for p in date_folder.rglob("*") if p.is_file())
            bytes_freed += size
            _safe_rmtree_in_trash(date_folder)
            purged.append(date_folder.name)
            log(f"PURGED expired trash folder {date_folder.name} ({human_size(size)})")
    return {"purged": purged, "bytes_freed": bytes_freed, "bytes_human": human_size(bytes_freed)}


def purge_all() -> dict:
    if not TRASH_DIR.exists():
        return {"purged_count": 0, "bytes_freed": 0}
    size = sum(p.stat().st_size for p in TRASH_DIR.rglob("*") if p.is_file())
    count = sum(1 for _ in TRASH_DIR.rglob("*.jsonl"))
    for child in list(TRASH_DIR.iterdir()):
        if child.is_symlink():
            child.unlink()
            continue
        if child.is_dir():
            _safe_rmtree_in_trash(child)
        else:
            child.unlink()
    log(f"PURGED ALL trash ({count} files, {human_size(size)})")
    return {"purged_count": count, "bytes_freed": size, "bytes_human": human_size(size)}


def render_summary() -> str:
    s = stats()
    b = bucket_stats()
    lines = [
        f"Total: {s['live_count']} sessions, {s['live_human']}",
    ]
    for key, label, _lo, _hi in AGE_BUCKETS:
        bk = b["buckets"][key]
        lines.append(f"  {label:<22}  {bk['count']} files, {bk['bytes_human']}")
    lines.append(f"  Protected (running):    {s['current_count']}")
    if s["trash_count"] == 0:
        lines.append(f"  Trash:                  empty")
    else:
        expires_in = None
        if s.get("oldest_trash_date"):
            age = (_today_utc() - datetime.strptime(s["oldest_trash_date"], "%Y-%m-%d").date()).days
            expires_in = RETENTION_DAYS - age
        tail = f" (oldest {s['oldest_trash_date']}, expires in {expires_in} days)" if expires_in is not None else ""
        lines.append(f"  Trash:                  {s['trash_count']} file{'s' if s['trash_count'] != 1 else ''}, {s['trash_human']}{tail}")
    return "```\n" + "\n".join(lines) + "\n```"


def _format_entry(idx: int | str, it: dict, *, mode: str = "live") -> str:
    if mode == "live":
        timestamp = f"[{it['mtime']}]"
    else:  # trash
        timestamp = f"[trashed {it['trashed_date']} · expires in {it['expires_in_days']} days]"
    tag = ""
    if mode == "live" and it.get("is_current"):
        tag = "  ⚠️ PROTECTED (current session — unpickable)"
    elif mode == "trash" and it.get("expires_in_days", 99) <= 1:
        tag = "  ⚠️ EXPIRING"
    preview = (it.get("preview") or "").replace("\n", " ")
    if len(preview) > 80:
        preview = preview[:80]
    idx_str = f"{idx}" if isinstance(idx, str) else f"{idx:>2}"
    return (
        f"# {idx_str}  {timestamp}  {it['size_human']}{tag}\n"
        f"     {it['project_decoded']}\n"
        f"     {preview}"
    )


def render_live_page(page: int = 1, page_size: int = 20) -> dict:
    all_items = list_live(oldest_first=True)
    candidates = [x for x in all_items if not x["is_current"]]
    protected = [x for x in all_items if x["is_current"]]
    total = len(candidates)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * page_size
    page_items = candidates[offset : offset + page_size]
    end = offset + len(page_items)
    header = f"Page {page}/{total_pages} · showing {offset + 1}-{end} of {total} (oldest first)" if total else "No deletable sessions."
    entries_md: list[str] = []
    pickable: list[dict] = []
    for i, it in enumerate(page_items, 1):
        entries_md.append(_format_entry(i, it, mode="live"))
        pickable.append({"index": i, "path": it["path"], "size_human": it["size_human"]})
    # Show the protected session as a hint at the end of the last page (only
    # when there are candidates on this page, to avoid stacking below "(nothing)").
    if page == total_pages and protected and entries_md:
        for p in protected:
            entries_md.append(_format_entry(" —", p, mode="live"))
    body = "\n\n".join(entries_md) if entries_md else "(nothing)"
    markdown = f"{header}\n\n```\n{body}\n```"
    return {
        "markdown": markdown,
        "page": page,
        "total_pages": total_pages,
        "total_candidates": total,
        "entries": pickable,
    }


def render_trash_list() -> dict:
    items = list_trash()
    entries_md: list[str] = []
    mapped: list[dict] = []
    for i, it in enumerate(items, 1):
        entries_md.append(_format_entry(i, it, mode="trash"))
        mapped.append({"index": i, "path": it["path"], "size_human": it["size_human"]})
    body = "\n\n".join(entries_md) if entries_md else "(trash is empty)"
    n = len(items)
    markdown = f"Trash ({n} file{'s' if n != 1 else ''})\n\n```\n{body}\n```"
    return {
        "markdown": markdown,
        "total": len(items),
        "entries": mapped,
    }


def menu_options(context: str = "menu", page: int = 1, total_pages: int = 1) -> list[dict]:
    s = stats()
    b = bucket_stats()
    opts: list[dict] = []
    if context == "menu":
        archive = b["buckets"]["archive"]
        old = b["buckets"]["old"]
        recent = b["buckets"]["recent"]
        if archive["count"] > 0:
            opts.append({
                "label": "Delete everything older than 90 days (archive)",
                "description": f"{archive['count']} files, {archive['bytes_human']} — usually safe",
            })
        if old["count"] + archive["count"] > 0:
            n = old["count"] + archive["count"]
            bytes_h = human_size(old["bytes"] + archive["bytes"])
            opts.append({
                "label": "Delete everything older than 30 days",
                "description": f"{n} files, {bytes_h}",
            })
        if recent["count"] + old["count"] + archive["count"] > 0:
            n = recent["count"] + old["count"] + archive["count"]
            bytes_h = human_size(recent["bytes"] + old["bytes"] + archive["bytes"])
            opts.append({
                "label": "Delete everything older than 7 days",
                "description": f"{n} files, {bytes_h}",
            })
        opts.append({
            "label": "Browse & pick sessions to delete…",
            "description": f"Page through all {s['live_count'] - s['current_count']} deletable sessions",
        })
        if s["trash_count"] > 0:
            opts.append({
                "label": "Restore from trash…",
                "description": f"{s['trash_count']} file{'s' if s['trash_count'] != 1 else ''}, {s['trash_human']}",
            })
            opts.append({
                "label": "Empty trash permanently",
                "description": f"{s['trash_count']} file{'s' if s['trash_count'] != 1 else ''}, {s['trash_human']} — cannot be recovered",
            })
        cancel = {"label": "Do nothing — close", "description": "Exit"}
        # Cap 4. Always try to keep the cancel option.
        if len(opts) >= 4:
            bulk = next((o for o in opts if o["label"].startswith("Delete everything")), None)
            browse = next(o for o in opts if o["label"].startswith("Browse"))
            restore = next((o for o in opts if o["label"].startswith("Restore")), None)
            empty = next((o for o in opts if o["label"].startswith("Empty trash")), None)
            # Prioritise: one bulk, Browse, Restore, then pad with Empty trash or cancel
            ordered = [x for x in [bulk, browse, restore, empty, cancel] if x is not None]
            opts = ordered[:4]
            if not any(o["label"].startswith("Do nothing") for o in opts):
                # Drop the last non-cancel option to make room for cancel.
                opts = opts[:3] + [cancel]
        else:
            opts.append(cancel)
    elif context.startswith("browse"):
        opts.append({
            "label": "Pick from this page",
            "description": "Enter numbers to delete (e.g. 1,3,5-8)",
        })
        opts.append({
            "label": "Delete all on this page",
            "description": "Move every session shown on this page to trash",
        })
        has_next = page < total_pages
        has_prev = page > 1
        if total_pages > 1:
            # multi-page
            if has_next and has_prev:
                opts.append({"label": "Next page", "description": f"Go to page {page + 1} of {total_pages}"})
                opts.append({"label": "Previous page", "description": f"Go to page {page - 1} of {total_pages}"})
            elif has_next:
                opts.append({"label": "Next page", "description": f"Go to page {page + 1} of {total_pages}"})
                opts.append({"label": "Delete all (except protected)", "description": "Move every deletable session to trash"})
            else:  # has_prev only (last page)
                opts.append({"label": "Previous page", "description": f"Go to page {page - 1} of {total_pages}"})
                opts.append({"label": "Delete all (except protected)", "description": "Move every deletable session to trash"})
        else:
            opts.append({"label": "Back to menu", "description": "Return to the main menu"})
        if len(opts) < 4 and not any(o["label"] == "Back to menu" for o in opts):
            opts.append({"label": "Back to menu", "description": "Return to the main menu"})
    return opts[:4]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list-live")
    p_list.add_argument("--older-than-days", type=int, default=None)
    p_list.add_argument("--limit", type=int, default=None)
    p_list.add_argument("--offset", type=int, default=0)
    p_list.add_argument("--oldest-first", action="store_true")
    sub.add_parser("list-trash")
    sub.add_parser("detect-current")
    sub.add_parser("stats")
    sub.add_parser("bucket-stats")
    sub.add_parser("render-summary")
    p_rlp = sub.add_parser("render-live-page")
    p_rlp.add_argument("--page", type=int, default=1)
    p_rlp.add_argument("--page-size", type=int, default=20)
    sub.add_parser("render-trash-list")
    p_menu = sub.add_parser("menu-options")
    p_menu.add_argument("--context", choices=["menu", "browse"], default="menu")
    p_menu.add_argument("--page", type=int, default=1)
    p_menu.add_argument("--total-pages", type=int, default=1)
    sub.add_parser("purge-expired")
    sub.add_parser("purge-all")
    p_trash = sub.add_parser("trash")
    p_trash.add_argument("paths", nargs="+")
    p_trash_older = sub.add_parser("trash-older-than")
    p_trash_older.add_argument("days", type=int)
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("paths", nargs="+")
    args = parser.parse_args()

    if args.cmd == "list-live":
        print(json.dumps(
            list_live(
                older_than_days=args.older_than_days,
                limit=args.limit,
                offset=args.offset,
                oldest_first=args.oldest_first,
            ),
            indent=2,
        ))
    elif args.cmd == "list-trash":
        print(json.dumps(list_trash(), indent=2))
    elif args.cmd == "detect-current":
        print(json.dumps(detect_current_sessions(), indent=2))
    elif args.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.cmd == "bucket-stats":
        print(json.dumps(bucket_stats(), indent=2))
    elif args.cmd == "render-summary":
        print(render_summary())
    elif args.cmd == "render-live-page":
        print(json.dumps(render_live_page(args.page, args.page_size), indent=2))
    elif args.cmd == "render-trash-list":
        print(json.dumps(render_trash_list(), indent=2))
    elif args.cmd == "menu-options":
        print(json.dumps(menu_options(args.context, args.page, args.total_pages), indent=2))
    elif args.cmd == "trash":
        print(json.dumps(trash_files(args.paths), indent=2))
    elif args.cmd == "trash-older-than":
        print(json.dumps(trash_older_than(args.days), indent=2))
    elif args.cmd == "restore":
        print(json.dumps(restore_files(args.paths), indent=2))
    elif args.cmd == "purge-expired":
        print(json.dumps(purge_expired(), indent=2))
    elif args.cmd == "purge-all":
        print(json.dumps(purge_all(), indent=2))


if __name__ == "__main__":
    main()
