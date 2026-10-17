"""Scheduled job: poll subscriptions, then let a headless Claude Code post.

Run by the OS scheduler once a day at a quiet hour (Windows Task Scheduler,
04:00), e.g. ``pythonw -m bilibili_mcp.daily``. Each run:

* syncs the queue playlist (the user's YouTube playlist of videos to post);
* polls the followed YouTube channels and queues their new videos;
* records performance snapshots of posted videos (feedback.collect);
* posts every queued **subscription** video (no daily limit) and manually queued
  videos up to the weekday's limit (upload_queue.WEEKLY_AUTO_LIMITS: none
  Mon-Thu, two Fri/Sat, one Sun), scheduled to go public at PUBLISH_HOUR that
  day (Bilibili 定时发布), or right away when the run starts too late for that;
* on a run with nothing to post, about once a week when enough videos have
  matured, runs a feedback review: Claude reads the stats and may adjust the
  style guide's feedback section within the bounds written there.

Polling is plain Python; Claude is only started when there is something to
post or review, because those are the parts that need judgment.

The base prompt comes from ``<home>/daily_prompt.md`` when present (keep
channel-specific instructions there, outside the repo), else BASE_PROMPT; the
run's claim instructions are appended. The review prompt likewise comes from
``<home>/review_prompt.md``, else REVIEW_PROMPT. Each run logs to
``<home>/daily/<date>.log``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

from bilibili_mcp import feedback, upload_queue
from bilibili_mcp.server import feedback_store, home, store, web_get

# Videos uploaded by the run go public at this local hour the same day.
PUBLISH_HOUR = int(os.environ.get("BILIBILI_MCP_PUBLISH_HOUR", "18"))
# Bilibili wants a scheduled publish time at least 4 hours ahead; keep a margin
# for the downloads and uploads that happen before upload_video is called.
SCHEDULE_MARGIN = timedelta(hours=6)

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
5. Wait for mcp__bilibili__upload_status to report done, verify with video_info, record the
   title's style and borrowed meme with mcp__bilibili__feedback_tag, then call
   queue_finish with status=done and the bvid. On any failure call queue_finish with
   status=failed and the error. Delete the downloaded files.
Finish with one line per item: BV id and title, or why it was not posted.
"""

REVIEW_PROMPT = """\
You are the weekly feedback reviewer for this Bilibili channel.
1. Call mcp__bilibili__feedback_report and read channel_style.md in the working directory.
2. Look for patterns that hold across at least 3 videos (title style, meme vs none, artist
   origin, title length...). Ignore single outliers; who the artist is often matters more
   than the title, so compare like with like.
3. Edit ONLY the section of channel_style.md titled "数据反馈" and stay within the bounds
   written there. If the evidence is thin, change nothing; that is a fine outcome.
4. Append a dated entry to feedback/changes.md in the working directory: what the data
   showed, what you changed (quote old -> new) or why you changed nothing.
Finish with a 2-3 line summary.
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
    slots = upload_queue.daily_auto_limit(now) - upload_queue.auto_posts_today(data)
    lines += ["- the oldest manually queued item: queue_claim(auto=true)"] * min(slots, len(manual))
    return lines


def publish_note(now: datetime) -> str:
    """When the run's uploads should go public."""
    at = now.replace(hour=PUBLISH_HOUR, minute=0, second=0, microsecond=0)
    if at - now < SCHEDULE_MARGIN:
        return "Publish each upload right away (leave publish_at empty).\n"
    return f'Pass publish_at="{at:%Y-%m-%d %H:%M}" to upload_video so every upload goes public then.\n'


def run_claude(prompt: str) -> int:
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


def review() -> int:
    """Weekly feedback review: Claude reads the stats and may tune the style guide."""
    prompt_file = home() / "review_prompt.md"
    prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else REVIEW_PROMPT
    (home() / "feedback").mkdir(exist_ok=True)
    log("feedback review")
    code = run_claude(prompt)
    if code == 0:
        feedback.mark_reviewed(feedback_store())
    return code


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
        try:
            snap = feedback.collect(feedback_store(), web_get)
            if snap["new_videos"] or snap["snapshots"]:
                log(f"feedback: {json.dumps(snap)}")
        except Exception as exc:  # stats are a side task; never block posting
            log(f"feedback error: {exc!r}")
        now = datetime.now()
        claims = plan(store().load(), now)
        if not claims:
            if feedback.review_due(feedback_store()):
                if dry_run:
                    log("dry run: feedback review due")
                    return 0
                return review()
            return 0
        prompt_file = home() / "daily_prompt.md"
        prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else BASE_PROMPT
        prompt += "\n" + publish_note(now)
        prompt += "\nItems to claim in this run:\n" + "\n".join(claims) + "\n"
        log("posting:\n" + "\n".join(claims))
        if dry_run:
            log("dry run: not starting claude")
            return 0
        (home() / "work").mkdir(exist_ok=True)
        return run_claude(prompt)
    except Exception as exc:  # the scheduler shows nothing; the log is the only trace
        log(f"error: {exc!r}")
        return 1
    finally:
        lock.unlink(missing_ok=True)


def main() -> None:
    sys.exit(run(dry_run="--dry-run" in sys.argv))


if __name__ == "__main__":
    main()
