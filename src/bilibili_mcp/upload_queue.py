"""Upload queue and YouTube channel subscriptions.

Plain state, no judgment: the queue only remembers *which* videos to post and
how far each got. Writing titles and descriptions stays with the agent.

State lives in ``<home>/queue.json``:

    {"items": [...], "subscriptions": [...], "playlist": {...}}

The user manages manual items in a YouTube playlist on their own account:
``sync_playlist`` queues videos added there (origin "playlist"), follows its
order, and drops pending items that were removed from it. With YouTube OAuth
(youtube.py) the playlist is read through the API, ``add`` also puts the video
into it, and a posted video is moved to the "posted" playlist; without it the
playlist is read by yt-dlp and must be public or unlisted.

A second playlist, the priority playlist (origin "priority"), works the same
way; its videos are handed out before the rest of the manual queue. They are
ones the user rates highly, so the agent may title them more warmly.

An item moves pending -> processing -> done | failed | skipped. Manually queued
items are posted through ``claim_next(auto=True)``, at most daily_auto_limit()
a day (set per weekday in WEEKLY_AUTO_LIMITS). Subscription videos, and anything the user asks to post now, are claimed
with ``auto=False`` and do not count toward that limit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from bilibili_mcp.youtube import YouTube

YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/)([0-9A-Za-z_-]{11})")
CHANNEL_ID_RE = re.compile(r"\bUC[0-9A-Za-z_-]{22}\b")
PLAYLIST_ID_RE = re.compile(r"(?:list=)?\b((?:PL|OL|UU)[0-9A-Za-z_-]{10,})")
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# Manually queued items the daily job may post per weekday (Monday = 0): none
# Monday to Thursday, two on Friday and Saturday, one on Sunday.
WEEKLY_AUTO_LIMITS = (0, 0, 0, 0, 2, 2, 1)
POSTED_PLAYLIST_TITLE = "B站已投"
PRIORITY_PLAYLIST_TITLE = "B站优先队列"
# Store key of each linked playlist -> origin of the items it queues. Priority comes
# last so that a video in both playlists ends up a priority item.
PLAYLISTS = {"playlist": "playlist", "priority_playlist": "priority"}
# A claimed item whose agent died is handed out again after this long.
STALE_PROCESSING = timedelta(hours=3)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"items": [], "subscriptions": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def update(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        data = self.load()
        result = fn(data)
        self.save(data)
        return result


def video_id(url: str) -> str:
    m = YT_ID_RE.search(url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", url.strip()):
        return url.strip()
    raise ToolError(f"not a YouTube video URL: {url}")


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


# --------------------------------------------------------------------- queue


def youtube(store: Store) -> YouTube | None:
    """The OAuth client when the user has logged in, else None (read-only mode)."""
    yt = YouTube(store.path.parent)
    return yt if yt.logged_in() else None


def priority_playlist(store: Store, yt: YouTube) -> dict[str, Any]:
    """The linked priority playlist, created (unlisted) on first use."""
    pl = store.load().get("priority_playlist")
    if not pl:
        pl = {"id": yt.create_playlist(PRIORITY_PLAYLIST_TITLE), "title": PRIORITY_PLAYLIST_TITLE}
        store.update(lambda data: data.update(priority_playlist=pl))
    return pl


def add(store: Store, url: str, title: str = "", origin: str = "manual", note: str = "",
        priority: bool = False) -> dict[str, Any]:
    vid = video_id(url)
    data = store.load()
    existing = next((i for i in data["items"] if i["video_id"] == vid), None)
    yt = youtube(store) if origin == "manual" else None
    if priority:
        if yt and not (existing and existing["status"] not in ("pending", "failed")):
            yt.add_to_playlist(priority_playlist(store, yt)["id"], vid)
        origin = "priority"
    elif yt and data.get("playlist") and not existing:
        yt.add_to_playlist(data["playlist"]["id"], vid)  # the playlist stays the one place the user looks
        origin = "playlist"

    def fn(data: dict[str, Any]) -> dict[str, Any]:
        for item in data["items"]:
            if item["video_id"] == vid:
                if priority and item["status"] in ("pending", "failed"):
                    item.update(origin="priority", pos=-1)  # promoted: post it before the others
                    return {**item, "duplicate": True, "promoted": True}
                return {**item, "duplicate": True}
        item = {"id": uuid.uuid4().hex[:6], "video_id": vid, "url": watch_url(vid), "title": title,
                "origin": origin, "note": note, "status": "pending", "added": _now()}
        data["items"].append(item)
        return item

    return store.update(fn)


def _release_stale(data: dict[str, Any]) -> None:
    cutoff = datetime.now() - STALE_PROCESSING
    for item in data["items"]:
        if item["status"] == "processing" and datetime.fromisoformat(item["claimed"]) < cutoff:
            item["status"] = "pending"
            item["note"] = (item.get("note", "") + " [released: processing timed out]").strip()


def daily_auto_limit(day: datetime | None = None) -> int:
    return WEEKLY_AUTO_LIMITS[(day or datetime.now()).weekday()]


def auto_posts_today(data: dict[str, Any]) -> int:
    today = datetime.now().date().isoformat()
    return sum(1 for i in data["items"]
               if i.get("auto") and i["status"] in ("processing", "done") and i.get("claimed", "")[:10] == today)


def claim_next(store: Store, auto: bool, item_id: str = "") -> dict[str, Any]:
    """Hand out one pending item (oldest first, or `item_id`) and mark it processing."""

    def fn(data: dict[str, Any]) -> dict[str, Any]:
        _release_stale(data)
        limit = daily_auto_limit()
        if auto and auto_posts_today(data) >= limit:
            return {"claimed": None, "reason": f"today's auto limit ({limit}) reached"}
        pending = [i for i in data["items"] if i["status"] == "pending"]
        # priority items first, then playlist order, stable otherwise
        pending.sort(key=lambda i: (i["origin"] != "priority", i.get("pos", float("inf"))))
        if auto:  # subscription videos are posted on detection, outside the daily quota
            pending = [i for i in pending if not i["origin"].startswith("subscription:")]
        if item_id:
            pending = [i for i in data["items"] if i["id"] == item_id and i["status"] in ("pending", "failed")]
        if not pending:
            return {"claimed": None, "reason": "no pending item" if not item_id else f"no claimable item {item_id}"}
        item = pending[0]
        item.update(status="processing", claimed=_now(), auto=auto)
        return {"claimed": item}

    return store.update(fn)


def finish(store: Store, item_id: str, status: str, bvid: str = "", note: str = "") -> dict[str, Any]:
    if status not in ("done", "failed", "skipped", "pending"):
        raise ToolError("status must be done, failed, skipped or pending")

    def fn(data: dict[str, Any]) -> dict[str, Any]:
        for item in data["items"]:
            if item["id"] == item_id:
                item.update(status=status, finished=_now())
                if bvid:
                    item["bvid"] = bvid
                if note:
                    item["note"] = note
                return dict(item)
        raise ToolError(f"unknown queue item: {item_id}")

    item = store.update(fn)
    if status == "done" and item["origin"] in ("playlist", "priority"):
        try:
            item["moved_to"] = move_posted(store, item["video_id"])
        except Exception as exc:  # the upload itself succeeded; just report it
            item["move_error"] = str(exc)
    return item


def remove(store: Store, item_id: str) -> dict[str, Any]:
    def fn(data: dict[str, Any]) -> dict[str, Any]:
        for i, item in enumerate(data["items"]):
            if item["id"] == item_id:
                return data["items"].pop(i)
        raise ToolError(f"unknown queue item: {item_id}")

    return store.update(fn)


# ------------------------------------------------------------- subscriptions


def resolve_channel(ref: str) -> dict[str, str]:
    """Channel id and name from a channel URL, @handle or UC... id."""
    ref = ref.strip()
    if CHANNEL_ID_RE.fullmatch(ref):
        url = f"https://www.youtube.com/channel/{ref}"
    elif ref.startswith("@"):
        url = f"https://www.youtube.com/{ref}"
    elif ref.startswith("http"):
        url = ref
    else:
        url = f"https://www.youtube.com/@{ref}"
    headers = {"User-Agent": UA, "Accept-Language": "en"}
    with httpx.Client(headers=headers, timeout=20, follow_redirects=True,
                      cookies={"CONSENT": "YES+1", "SOCS": "CAI"}) as client:
        html = client.get(url).text
    m = re.search(r'"externalId":"(UC[0-9A-Za-z_-]{22})"', html) or \
        re.search(r'<meta itemprop="identifier" content="(UC[0-9A-Za-z_-]{22})"', html) or \
        re.search(r'"channelId":"(UC[0-9A-Za-z_-]{22})"', html)
    if not m:
        raise ToolError(f"could not find a channel id on {url}")
    name = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    return {"channel_id": m.group(1), "name": name.group(1) if name else ref}


def fetch_feed(channel_id: str) -> list[dict[str, str]]:
    """Latest uploads of a channel (YouTube RSS, newest first, ~15 entries)."""
    resp = httpx.get(FEED.format(channel_id), headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    out = []
    for entry in root.findall("a:entry", NS):
        link = entry.find("a:link", NS)
        out.append({
            "video_id": entry.findtext("yt:videoId", namespaces=NS),
            "title": entry.findtext("a:title", namespaces=NS) or "",
            "published": entry.findtext("a:published", namespaces=NS) or "",
            "url": link.get("href") if link is not None else "",
        })
    return out


def subscribe(store: Store, ref: str) -> dict[str, Any]:
    ch = resolve_channel(ref)
    # Mark everything already published as seen: a subscription only picks up new videos.
    seen = [v["video_id"] for v in fetch_feed(ch["channel_id"])]

    def fn(data: dict[str, Any]) -> dict[str, Any]:
        for sub in data["subscriptions"]:
            if sub["channel_id"] == ch["channel_id"]:
                return {**sub, "duplicate": True}
        sub = {**ch, "added": _now(), "seen": seen, "last_checked": _now()}
        data["subscriptions"].append(sub)
        return sub

    sub = store.update(fn)
    return {k: v for k, v in sub.items() if k != "seen"}


def unsubscribe(store: Store, ref: str) -> dict[str, Any]:
    def fn(data: dict[str, Any]) -> dict[str, Any]:
        for i, sub in enumerate(data["subscriptions"]):
            if ref in (sub["channel_id"], sub["name"]) or ref.lstrip("@").lower() == sub["name"].lower():
                data["subscriptions"].pop(i)
                return {"removed": sub["name"], "channel_id": sub["channel_id"]}
        raise ToolError(f"no subscription matches {ref}")

    return store.update(fn)


def check_subscriptions(store: Store) -> dict[str, Any]:
    """Queue every video a subscribed channel published since it was last seen."""
    subs = store.load()["subscriptions"]
    added, errors = [], []
    for sub in subs:
        try:
            feed = fetch_feed(sub["channel_id"])
        except Exception as exc:  # one dead feed must not stop the rest
            errors.append({"channel": sub["name"], "error": str(exc)})
            continue
        fresh = [v for v in feed if v["video_id"] not in set(sub["seen"])]
        for v in reversed(fresh):  # oldest first, so the queue keeps publish order
            item = add(store, v["url"] or watch_url(v["video_id"]), v["title"], origin=f"subscription:{sub['name']}")
            if not item.get("duplicate"):
                added.append({"id": item["id"], "title": v["title"], "channel": sub["name"]})

        def mark(data: dict[str, Any], sub=sub, feed=feed) -> None:
            for s in data["subscriptions"]:
                if s["channel_id"] == sub["channel_id"]:
                    s["seen"] = list(dict.fromkeys(s["seen"] + [v["video_id"] for v in feed]))[-200:]
                    s["last_checked"] = _now()

        store.update(mark)
    return {"checked": len(subs), "added": added, "errors": errors}


# ------------------------------------------------------------------ playlist


def playlist_id(ref: str) -> str:
    m = PLAYLIST_ID_RE.search(ref.strip())
    if not m:
        raise ToolError(f"not a YouTube playlist URL or id: {ref}")
    return m.group(1)


def fetch_playlist(pid: str, yt: YouTube | None = None) -> dict[str, Any]:
    """Title and video ids of a playlist, in playlist order (API if logged in, else yt-dlp)."""
    if yt:
        return {"title": yt.playlist_title(pid), "videos": yt.playlist_items(pid)}
    exe = os.environ.get("BILIBILI_MCP_YTDLP") or shutil.which("yt-dlp")
    if not exe:
        raise ToolError("yt-dlp not found on PATH (set BILIBILI_MCP_YTDLP)")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # uv's venv launcher sets PYTHONHOME, which would point yt-dlp's own Python at ours.
    # PYTHONIOENCODING: its JSON would otherwise come out in the Windows code page.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "UV_INTERNAL__PYTHONHOME", "VIRTUAL_ENV")}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(  # noqa: S603
        [exe, "--no-update", "--flat-playlist", "-J", f"https://www.youtube.com/playlist?list={pid}"],
        capture_output=True, timeout=120, creationflags=flags, stdin=subprocess.DEVNULL, env=env,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"could not read playlist {pid}: {err[-500:]}")
    info = json.loads(proc.stdout.decode("utf-8"))
    videos = [{"video_id": e["id"], "title": e.get("title") or ""}
              for e in info.get("entries") or [] if e and e.get("id")]
    return {"title": info.get("title") or "", "videos": videos}


def set_playlist(store: Store, ref: str, priority: bool = False) -> dict[str, Any]:
    key = "priority_playlist" if priority else "playlist"
    pid = playlist_id(ref)
    info = fetch_playlist(pid, youtube(store))  # fail now if it is unreadable or wrong
    store.update(lambda data: data.update({key: {**(data.get(key) or {}), "id": pid, "title": info["title"]}}))
    return {"id": pid, "title": info["title"], "videos": len(info["videos"]), **sync_playlist(store)}


def sync_playlist(store: Store) -> dict[str, Any]:
    """Mirror the queue and priority playlists into the queue: add new videos, follow
    their order, and drop pending playlist items that are in neither any more. A video
    in the priority playlist becomes a priority item. Posted videos may stay in a playlist."""
    linked = {key: pl for key in PLAYLISTS if (pl := store.load().get(key))}
    if not linked:
        return {"playlist": None}
    yt = youtube(store)
    wanted: dict[str, tuple[str, int, str]] = {}  # video id -> (origin, pos, title)
    for key, pl in linked.items():
        for n, v in enumerate(fetch_playlist(pl["id"], yt)["videos"]):
            wanted[v["video_id"]] = (PLAYLISTS[key], n, v["title"])

    def fn(data: dict[str, Any]) -> dict[str, Any]:
        added, removed = [], []
        known = {i["video_id"]: i for i in data["items"]}
        for vid, (origin, pos, title) in wanted.items():
            item = known.get(vid)
            if item is None:
                item = {"id": uuid.uuid4().hex[:6], "video_id": vid, "url": watch_url(vid), "title": title,
                        "origin": origin, "note": "", "status": "pending", "added": _now()}
                data["items"].append(item)
                added.append({"id": item["id"], "title": title, "origin": origin})
            elif item["origin"] in ("manual", *PLAYLISTS.values()) and item["status"] in ("pending", "failed"):
                item["origin"] = origin  # adopted: the playlists now decide its fate
            if item["status"] in ("pending", "failed"):
                item["pos"] = pos
        for item in list(data["items"]):
            if (item["origin"] in PLAYLISTS.values() and item["status"] in ("pending", "failed")
                    and item["video_id"] not in wanted):
                data["items"].remove(item)
                removed.append({"id": item["id"], "video_id": item["video_id"]})
        for key in linked:
            data[key]["last_synced"] = _now()
        return {"added": added, "removed": removed}

    return store.update(fn)


def move_posted(store: Store, vid: str) -> str:
    """Move a posted video from the queue playlists to the posted playlist (created on first use)."""
    yt = youtube(store)
    data = store.load()
    pl = data.get("playlist")
    if not yt or not pl:
        return ""
    posted = pl.get("posted_id")
    if not posted:
        posted = yt.create_playlist(POSTED_PLAYLIST_TITLE)
        store.update(lambda data: data["playlist"].update(posted_id=posted))
    yt.add_to_playlist(posted, vid)  # add first: a failure in between must not lose the video
    for key in PLAYLISTS:
        if data.get(key):
            yt.remove_from_playlist(data[key]["id"], vid)
    return posted
