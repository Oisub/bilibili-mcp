"""Daily job: poll subscriptions, then let a headless Claude Code post one queued video.

Run by the OS scheduler (Windows Task Scheduler), e.g. ``pythonw -m bilibili_mcp.daily``.
Polling is plain Python; Claude is only started when there is something to
post and today's auto quota is unused, because writing the title and
description is the part that needs judgment.

The prompt comes from ``<home>/daily_prompt.md`` when present (keep channel-
specific instructions there, outside the repo), else DEFAULT_PROMPT.
Each run appends to ``<home>/daily/<date>.log``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

from bilibili_mcp import upload_queue
from bilibili_mcp.server import home, store

DEFAULT_PROMPT = """\
You are the scheduled daily uploader for this Bilibili channel.
1. Call mcp__bilibili__queue_claim with auto=true. If nothing is claimed, stop.
2. Inspect the claimed YouTube video with yt-dlp (`yt-dlp --no-update --no-playlist -J <url>`).
   If it is not worth posting (a Short, a trailer without music, an upcoming premiere,
   off-topic), call queue_finish with status=skipped and a reason, then stop.
3. Download it (1080p H.264 + m4a merged to mp4, plus the thumbnail as jpg) into a new
   folder under the working directory's `work/` folder. If YouTube returns 403, upgrade
   yt-dlp (`python -m pip install -U "yt-dlp[default]"`) and retry.
4. Write the metadata following channel_style.md in the working directory, if present
   (including its title workflow with mcp__bilibili__trending), and upload it with
   mcp__bilibili__upload_video as a repost with the YouTube URL as source.
5. Wait for mcp__bilibili__upload_status to report done, verify with video_info, then call
   queue_finish with status=done and the bvid. On any failure call queue_finish with
   status=failed and the error. Delete the downloaded files at the end.
Finish with a two-line summary: BV id and title, or why nothing was posted.
"""

ALLOWED_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "mcp__bilibili"]


def claude_exe() -> str:
    override = os.environ.get("BILIBILI_MCP_CLAUDE")
    found = override or shutil.which("claude.cmd") or shutil.which("claude")
    if not found:
        raise RuntimeError("claude CLI not found on PATH (set BILIBILI_MCP_CLAUDE)")
    return found


def log(msg: str) -> None:
    folder = home() / "daily"
    folder.mkdir(exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    with open(folder / f"{datetime.now().date().isoformat()}.log", "a", encoding="utf-8") as fh:
        fh.write(line)


def run(dry_run: bool = False) -> int:
    lock = home() / "daily.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = datetime.now().timestamp() - lock.stat().st_mtime
        if age < 3 * 3600:
            log("another run holds daily.lock; exiting")
            return 0
        lock.unlink(missing_ok=True)  # left over from a crashed run
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        result = upload_queue.check_subscriptions(store())
        log(f"subscriptions: {json.dumps(result, ensure_ascii=False)}")
        data = store().load()
        pending = [i for i in data["items"] if i["status"] == "pending"]
        if upload_queue.auto_posts_today(data) >= upload_queue.DAILY_AUTO_LIMIT:
            log("daily auto limit already used; not posting")
            return 0
        if not pending:
            log("queue empty; not posting")
            return 0
        prompt_file = home() / "daily_prompt.md"
        prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else DEFAULT_PROMPT
        cmd = [claude_exe(), "-p", prompt, "--allowedTools", *ALLOWED_TOOLS]
        log(f"starting claude for {len(pending)} pending item(s)")
        if dry_run:
            log("dry run: not starting claude")
            return 0
        (home() / "work").mkdir(exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(  # noqa: S603
            cmd, cwd=home(), capture_output=True, timeout=3 * 3600, creationflags=flags,
            stdin=subprocess.DEVNULL,
        )
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        log(f"claude exited {proc.returncode}\n{out}" + (f"\nstderr: {err[-2000:]}" if err else ""))
        return proc.returncode
    except Exception as exc:  # the scheduler shows nothing; the log is the only trace
        log(f"error: {exc!r}")
        return 1
    finally:
        lock.unlink(missing_ok=True)


def main() -> None:
    sys.exit(run(dry_run="--dry-run" in sys.argv))


if __name__ == "__main__":
    main()
