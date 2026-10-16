"""Scheduled job: poll subscriptions, then let a headless Claude Code post.

Run by the OS scheduler every hour (Windows Task Scheduler), e.g.
``pythonw -m bilibili_mcp.daily``. Each run:

* syncs the queue playlist (the user's YouTube playlist of videos to post);
* polls the followed YouTube channels and queues their new videos;
* posts every queued **subscription** video right away (no daily limit);
* from POST_HOUR on, posts manually queued videos up to the weekday's limit
  (upload_queue.WEEKLY_AUTO_LIMITS: none Mon-Thu, two Fri/Sat, one Sun).

Polling is plain Python; Claude is only started when there is something to
post, because writing the title and description is the part that needs judgment.

The base prompt comes from ``<home>/daily_prompt.md`` when present (keep
channel-specific instructions there, outside the repo), else BASE_PROMPT; the
run's claim instructions are appended. Each run logs to ``<home>/daily/<date>.log``.
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

# Manually queued videos are posted from this local hour on (on allowed weekdays).
POST_HOUR = int(os.environ.get("BILIBILI_MCP_POST_HOUR", "18"))

BASE_PROMPT = """\
You are the scheduled uploader for this Bilibili channel. For each queue item you are told
to claim below, in order:
1. Claim it with mcp__bilibili__queue_claim exactly as instructed. If nothing is claimed,
   go on to the next one.
2. Inspect the YouTube video with yt-dlp (`yt-dlp --no-update --no-playlist -J <url>`).
   If it is not worth posting (a Short, a trailer without music, an upcoming premiere,
   off-topic), call queue_finish with status=skipped and a reason, and move on.
3. Download it (1080p H.264 + m4a merged to mp4, plus the thumbnail as jpg) into a new
   folder under the working directory's `work/` folder. If YouTube returns 403, upgrade
   yt-dlp (`python -m pip install -U "yt-dlp[default]"`) and retry.
4. Write the metadata following channel_style.md in the working directory, if present
   (including its title workflow with mcp__bilibili__memes and mcp__bilibili__trending), and upload it with
   mcp__bilibili__upload_video as a repost with the YouTube URL as source.
5. Wait for mcp__bilibili__upload_status to report done, verify with video_info, then call
   queue_finish with status=done and the bvid. On any failure call queue_finish with
   status=failed and the error. Delete the downloaded files.
Finish with one line per item: BV id and title, or why it was not posted.
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


def plan(data: dict, now: datetime) -> list[str]:
    """Claim instructions for this run, one line per item."""
    pending = [i for i in data["items"] if i["status"] == "pending"]
    lines = [f"- item {i['id']} ({i['url']}): queue_claim(auto=false, item_id=\"{i['id']}\")"
             for i in pending if i["origin"].startswith("subscription:")]
    manual = [i for i in pending if not i["origin"].startswith("subscription:")]
    if now.hour >= POST_HOUR:
        slots = upload_queue.daily_auto_limit(now) - upload_queue.auto_posts_today(data)
        lines += ["- the oldest manually queued item: queue_claim(auto=true)"] * min(slots, len(manual))
    return lines


def run(dry_run: bool = False) -> int:
    lock = home() / "daily.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = datetime.now().timestamp() - lock.stat().st_mtime
        if age < 3 * 3600:
            return 0  # a previous run (or its Claude) is still working
        lock.unlink(missing_ok=True)  # left over from a crashed run
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        try:
            synced = upload_queue.sync_playlist(store())
            if synced.get("added") or synced.get("removed"):
                log(f"playlist: {json.dumps(synced, ensure_ascii=False)}")
        except Exception as exc:  # a bad playlist read must not block subscriptions
            log(f"playlist error: {exc!r}")
        result = upload_queue.check_subscriptions(store())
        if result["added"] or result["errors"]:
            log(f"subscriptions: {json.dumps(result, ensure_ascii=False)}")
        claims = plan(store().load(), datetime.now())
        if not claims:
            return 0
        prompt_file = home() / "daily_prompt.md"
        prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else BASE_PROMPT
        prompt += "\nItems to claim in this run:\n" + "\n".join(claims) + "\n"
        log("posting:\n" + "\n".join(claims))
        if dry_run:
            log("dry run: not starting claude")
            return 0
        (home() / "work").mkdir(exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(  # noqa: S603
            [claude_exe(), "-p", prompt, "--allowedTools", *ALLOWED_TOOLS],
            cwd=home(), capture_output=True, timeout=3 * 3600, creationflags=flags,
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
