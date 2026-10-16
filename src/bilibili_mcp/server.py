"""MCP server for uploading videos to Bilibili, built on biliup.

Design notes
------------
biliup does the hard part (chunked upload, upload lines, the submit API).
This server wraps it in three ways:

* Login uses the ``stream_gears`` bindings directly: the QR code is rendered
  to a PNG and opened on the desktop, so the user scans it with the Bilibili
  app. The login secret only ever lives in the cookie file, never in chat.
* Uploads run the ``biliup`` CLI as a background process. A large video takes
  minutes, far longer than an MCP call should block, and the CLI prints the
  resulting BV id (the Python ``upload()`` binding returns nothing). Each job
  writes a log and a small JSON record under ``<home>/jobs``.
* Read-only views (account, archive list, zones) call the creator-center web
  APIs with the same cookies, because they return structured JSON with review
  state and rejection reasons that the CLI only prints as text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
# Only ToolError text reaches the client; any other exception is reported as a bare
# "Error executing tool <name>", so every anticipated failure raises ToolError.
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from bilibili_mcp import feedback, upload_queue

mcp = MCPServer("bilibili")
logging.getLogger("httpx").setLevel(logging.WARNING)

READ_ONLY = ToolAnnotations(readOnlyHint=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Bilibili requires a scheduled publish time to be at least this far ahead.
MIN_SCHEDULE_AHEAD = 4 * 3600
QR_LOGIN_TIMEOUT = 180.0


# --------------------------------------------------------------------- paths


def home() -> Path:
    override = os.environ.get("BILIBILI_MCP_HOME")
    path = Path(override) if override else Path.home() / ".bilibili-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cookie_file() -> Path:
    return home() / "cookies.json"


def jobs_dir() -> Path:
    path = home() / "jobs"
    path.mkdir(exist_ok=True)
    return path


def biliup_exe() -> str:
    exe = Path(sys.executable).with_name("biliup.exe" if os.name == "nt" else "biliup")
    if not exe.exists():
        raise ToolError(f"biliup CLI not found next to the Python interpreter ({exe})")
    return str(exe)


# ------------------------------------------------------------------- cookies


def load_cookies() -> dict[str, str]:
    path = cookie_file()
    if not path.exists():
        raise ToolError("Not logged in. Call the `login` tool first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    cookies = data.get("cookie_info", {}).get("cookies", [])
    jar = {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    if "SESSDATA" not in jar:
        raise ToolError("Cookie file has no SESSDATA. Call `login` again.")
    return jar


def web_get(url: str, params: dict[str, Any] | None = None, *, cookies: dict[str, str] | None = None,
            referer: str = "https://member.bilibili.com/") -> Any:
    """GET a Bilibili web API with the saved cookies (plus ``cookies``) and unwrap ``data``."""
    headers = {"User-Agent": UA, "Referer": referer}
    with httpx.Client(cookies={**load_cookies(), **(cookies or {})}, headers=headers, timeout=20) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
    if body.get("code") != 0:
        raise ToolError(f"Bilibili API error {body.get('code')}: {body.get('message')}")
    return body.get("data")


# WBI request signing: some endpoints answer -352 without it. The mixing table is
# fixed by Bilibili's web client; the keys come from the nav API and rotate daily.
_WBI_MIXIN = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def wbi_get(url: str, params: dict[str, Any], **kwargs: Any) -> Any:
    """`web_get` with a WBI signature (``w_rid`` / ``wts``) added to the query."""
    import hashlib
    from urllib.parse import urlencode

    img = web_get("https://api.bilibili.com/x/web-interface/nav").get("wbi_img", {})
    raw = Path(img["img_url"]).stem + Path(img["sub_url"]).stem
    key = "".join(raw[i] for i in _WBI_MIXIN)[:32]
    signed = {**params, "wts": int(time.time())}
    signed = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(signed.items())}
    signed["w_rid"] = hashlib.md5((urlencode(signed) + key).encode()).hexdigest()  # noqa: S324 - protocol-mandated
    return web_get(url, signed, **kwargs)


def whoami() -> dict[str, Any]:
    data = web_get("https://api.bilibili.com/x/web-interface/nav")
    if not data.get("isLogin"):
        raise ToolError("Cookies expired. Call `login` again.")
    return {"mid": data.get("mid"), "name": data.get("uname"), "level": data.get("level_info", {}).get("current_level")}


# --------------------------------------------------------------------- login


@mcp.tool(annotations=READ_ONLY)
def account() -> dict[str, Any]:
    """Show which Bilibili account is logged in and whether its cookies still work."""
    if not cookie_file().exists():
        return {"logged_in": False, "hint": "Call `login` to scan a QR code with the Bilibili app."}
    try:
        return {"logged_in": True, **whoami()}
    except Exception as exc:  # expired cookies, network, ...
        return {"logged_in": False, "error": str(exc)}


@mcp.tool()
async def login() -> dict[str, Any]:
    """Log in by QR code: opens a QR image on the desktop for the user to scan with the Bilibili app.

    Waits up to 3 minutes for the scan, then saves the cookies locally. Tell the
    user to scan the image that just opened (Bilibili app -> 扫一扫).
    """
    import segno
    import stream_gears

    qr = await asyncio.to_thread(stream_gears.get_qrcode)
    url = json.loads(qr)["data"]["url"]
    png = home() / "login_qr.png"
    segno.make(url, error="m").save(str(png), scale=10, border=4)
    if os.name == "nt":
        os.startfile(png)  # noqa: S606 - opens the image viewer for the user

    tmp = home() / "cookies.login.json"
    try:
        await asyncio.wait_for(
            asyncio.to_thread(stream_gears.login_by_qrcode, qr, None, str(tmp)),
            timeout=QR_LOGIN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return {"logged_in": False, "error": "QR code was not scanned within 3 minutes. Call `login` again."}
    finally:
        png.unlink(missing_ok=True)
    tmp.replace(cookie_file())
    return {"logged_in": True, **whoami()}


# ---------------------------------------------------------------------- jobs


def _job_meta_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.json"


def _write_meta(meta: dict[str, Any]) -> None:
    _job_meta_path(meta["job_id"]).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _read_log(job_id: str) -> str:
    path = jobs_dir() / f"{job_id}.log"
    if not path.exists():
        return ""
    return ANSI_RE.sub("", path.read_bytes().decode("utf-8", errors="replace"))


def _start_job(kind: str, args: list[str], summary: dict[str, Any], cleanup: Path | None = None) -> dict[str, Any]:
    job_id = datetime.now().strftime("%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    log = open(jobs_dir() / f"{job_id}.log", "wb")  # noqa: SIM115 - closed by the watcher thread
    cmd = [biliup_exe(), "-u", str(cookie_file()), *args]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(  # noqa: S603
        cmd, cwd=home(), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env={**os.environ, "NO_COLOR": "1"}, creationflags=flags,
    )
    meta = {"job_id": job_id, "kind": kind, "status": "running", "pid": proc.pid,
            "started": datetime.now().isoformat(timespec="seconds"), **summary}
    _write_meta(meta)

    def watch() -> None:
        code = proc.wait()
        log.close()
        if cleanup:
            import shutil
            shutil.rmtree(cleanup, ignore_errors=True)
        text = _read_log(job_id)
        bvids = BV_RE.findall(text)
        meta.update(status="done" if code == 0 else "failed", exit_code=code,
                    finished=datetime.now().isoformat(timespec="seconds"))
        if bvids:
            meta["bvid"] = bvids[-1]
        _write_meta(meta)

    threading.Thread(target=watch, daemon=True).start()
    return meta


def _metadata_args(
    title: str, tid: int, tags: list[str], desc: str, cover: str, copyright: int, source: str,
    publish_at: str, private: bool, dynamic: str, no_reprint: bool, tid_v2: int | None,
) -> list[str]:
    if not title.strip():
        raise ToolError("title is required")
    if len(title) > 80:
        raise ToolError("title must be 80 characters or fewer")
    if not tags:
        raise ToolError("at least one tag is required")
    if copyright == 2 and not source:
        raise ToolError("source is required for a repost (copyright=2)")
    if cover and not Path(cover).is_file():
        raise ToolError(f"cover image not found: {cover}")
    args = ["--title", title, "--tid", str(tid), "--tag", ",".join(tags), "--desc", desc,
            "--copyright", str(copyright), "--no-reprint", "1" if no_reprint else "0"]
    if tid_v2 is not None:
        args += ["--tid-v2", str(tid_v2)]
    if cover:
        args += ["--cover", str(Path(cover).resolve())]
    if source:
        args += ["--source", source]
    if dynamic:
        args += ["--dynamic", dynamic]
    if private:
        args += ["--is-only-self", "1"]
    if publish_at:
        ts = int(datetime.fromisoformat(publish_at).timestamp())
        if ts - time.time() < MIN_SCHEDULE_AHEAD:
            raise ToolError("publish_at must be at least 4 hours from now")
        args += ["--dtime", str(ts)]
    return args


# Bilibili keeps only the first 250 characters of a description. On upload (app
# submit) it prepends "<source>\n" to a repost's desc, which counts toward the limit.
DESC_LIMIT = 250


def _strip_source(desc: str, source: str) -> str:
    """Drop leading copies of the source link (Bilibili adds one itself)."""
    desc = desc.lstrip()
    while source and desc.startswith(source):
        desc = desc[len(source):].lstrip()
    return desc


def _check_desc_length(desc: str, source: str) -> None:
    used = len(desc) + (len(source) + 1 if source else 0)
    if used > DESC_LIMIT:
        budget = DESC_LIMIT - (len(source) + 1 if source else 0)
        raise ToolError(f"desc is {len(desc)} characters; Bilibili keeps only {budget} here "
                         f"({DESC_LIMIT} minus the auto-added source link). Shorten it.")


def _check_files(files: list[str]) -> list[str]:
    if not files:
        raise ToolError("files must list at least one video")
    resolved = []
    for f in files:
        p = Path(f)
        if not p.is_file():
            raise ToolError(f"video not found: {f}")
        resolved.append(str(p.resolve()))
    return resolved


# Characters Windows forbids in file names, mapped to their full-width forms.
_FULLWIDTH = str.maketrans('\\/:*?"<>|', "＼／：＊？＂＜＞｜")


def _titled_parts(paths: list[str], part_titles: list[str] | None) -> tuple[list[str], Path | None]:
    """Give each part its title. biliup names a part after its file stem, so link
    each file under ``<title><ext>`` in a temp folder (hard link, no copy)."""
    if not part_titles:
        return paths, None
    if len(part_titles) != len(paths):
        raise ToolError("part_titles must have one entry per file")
    names = [t.strip().translate(_FULLWIDTH).rstrip(". ") for t in part_titles]
    for i, name in enumerate(names):
        if not name or len(name) > 80:
            raise ToolError(f"part title {i + 1} must be 1-80 characters")
    folder = jobs_dir() / f"parts-{uuid.uuid4().hex[:8]}"
    out = []
    for i, (src, name) in enumerate(zip(paths, names)):
        sub = folder / str(i)  # one subfolder per part, so equal titles cannot collide
        sub.mkdir(parents=True)
        dst = sub / (name + Path(src).suffix)
        try:
            os.link(src, dst)
        except OSError:  # different volume: fall back to a symlink, then a copy
            try:
                dst.symlink_to(src)
            except OSError:
                import shutil
                shutil.copy2(src, dst)
        out.append(str(dst))
    return out, folder


# -------------------------------------------------------------------- upload


@mcp.tool()
def upload_video(
    files: list[str],
    title: str,
    tid: int,
    tags: list[str],
    desc: str = "",
    cover: str = "",
    copyright: int = 1,
    source: str = "",
    publish_at: str = "",
    private: bool = False,
    dynamic: str = "",
    no_reprint: bool = False,
    tid_v2: int | None = None,
    line: str = "",
    part_titles: list[str] | None = None,
) -> dict[str, Any]:
    """Upload and submit a new video (稿件). Runs in the background; poll with `upload_status`.

    Confirm title, zone, tags and visibility with the user before calling: this
    publishes to their real channel.

    files: local video paths; each file becomes one part (分P), in order.
    tid: zone id (分区) - look it up with `zones`.
    tags: 1-12 tags.
    cover: local image path; empty lets Bilibili pick a frame.
    copyright: 1 = original (自制), 2 = repost (转载, needs `source`).
    publish_at: scheduled publish time, local ISO format like "2026-10-20 20:00";
        must be at least 4 hours ahead. Empty publishes as soon as review passes.
    private: only visible to the uploader (仅自己可见).
    dynamic: text of the feed post (空间动态).
    no_reprint: forbid reposting.
    line: upload line override (e.g. bda2, tx, alia); empty lets biliup choose.
    part_titles: one title per file for the parts (分P标题). Empty: a single file takes
        the video title, several files keep their file names.
    For a repost, Bilibili prepends `source` to the description by itself, so a
    leading copy of the source link in `desc` is dropped.
    """
    paths = _check_files(files)
    if copyright == 2 and source:
        desc = _strip_source(desc, source)
    _check_desc_length(desc, source if copyright == 2 else "")
    if not part_titles and len(paths) == 1:
        part_titles = [title[:80]]
    args = ["upload", *_metadata_args(title, tid, tags, desc, cover, copyright, source,
                                       publish_at, private, dynamic, no_reprint, tid_v2)]
    if line:
        args += ["--line", line]
    upload_paths, tmp = _titled_parts(paths, part_titles)
    meta = _start_job("upload", [*args, *upload_paths], {"title": title, "files": paths}, tmp)
    return {"job_id": meta["job_id"], "status": "running",
            "hint": "Call upload_status with this job_id to follow progress."}


@mcp.tool()
def append_parts(bvid: str, files: list[str], line: str = "", part_titles: list[str] | None = None) -> dict[str, Any]:
    """Append new parts (分P) to an existing video of this account. Runs in the background.

    part_titles: one title per file (分P标题); empty uses the file names.
    """
    paths = _check_files(files)
    args = ["append", "--vid", bvid]
    if line:
        args += ["--line", line]
    upload_paths, tmp = _titled_parts(paths, part_titles)
    meta = _start_job("append", [*args, *upload_paths], {"bvid": bvid, "files": paths}, tmp)
    return {"job_id": meta["job_id"], "status": "running"}


@mcp.tool(annotations=READ_ONLY)
def upload_status(job_id: str = "", log_lines: int = 15) -> dict[str, Any]:
    """Status of upload/append jobs. With no job_id, lists the 10 most recent jobs.

    A finished upload carries its `bvid`. "done" means Bilibili accepted the
    submission; it still goes through review - check with `my_videos`.
    """
    if not job_id:
        metas = sorted(jobs_dir().glob("*.json"), reverse=True)[:10]
        return {"jobs": [json.loads(p.read_text(encoding="utf-8")) for p in metas]}
    path = _job_meta_path(job_id)
    if not path.exists():
        raise ToolError(f"unknown job_id: {job_id}")
    meta = json.loads(path.read_text(encoding="utf-8"))
    lines = [ln for ln in _read_log(job_id).splitlines() if ln.strip()]
    meta["log_tail"] = lines[-log_lines:]
    return meta


# --------------------------------------------------------------------- views


def _show(bvid: str) -> dict[str, Any]:
    """Full archive JSON of one of this account's videos, via `biliup show`."""
    proc = subprocess.run(  # noqa: S603
        [biliup_exe(), "-u", str(cookie_file()), "show", bvid], cwd=home(),
        capture_output=True, timeout=60, env={**os.environ, "NO_COLOR": "1"},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 or "{" not in out:
        err = ANSI_RE.sub("", (proc.stderr or proc.stdout).decode("utf-8", errors="replace"))
        raise ToolError(f"biliup show failed: {err.strip()[-500:]}")
    return json.loads(out[out.index("{"):])


@mcp.tool()
def edit_video(
    bvid: str,
    title: str | None = None,
    desc: str | None = None,
    tags: list[str] | None = None,
    part_titles: list[str] | None = None,
) -> dict[str, Any]:
    """Edit an already submitted video: title, description, tags or part titles.

    Only the given fields change. An edit sends the video back into review.
    For a repost, write `desc` without the source link: Bilibili adds it itself.
    part_titles: one per existing part, in order.
    """
    data = _show(bvid)
    arc = data["archive"]
    parts = data.get("videos") or []
    if part_titles is not None and len(part_titles) != len(parts):
        raise ToolError(f"part_titles must have {len(parts)} entries")
    new_title = title if title is not None else arc.get("title", "")
    new_desc = desc if desc is not None else arc.get("desc", "")
    if arc.get("copyright") == 2 and arc.get("source") and desc is not None:
        new_desc = _strip_source(new_desc, arc["source"])
    # Unlike the app submit used for uploads, the web edit does not prepend the source.
    _check_desc_length(new_desc, "")
    payload = {
        "aid": arc["aid"],
        "title": new_title,
        "tid": arc["tid"],
        "tag": ",".join(tags) if tags is not None else arc.get("tag", ""),
        "copyright": arc["copyright"],
        "source": arc.get("source", ""),
        "cover": arc.get("cover", ""),
        "desc": new_desc,
        "desc_format_id": arc.get("desc_format_id", 0),
        "dynamic": arc.get("dynamic", ""),
        "no_reprint": arc.get("no_reprint", 0),
        "is_only_self": arc.get("is_only_self", 0),
        "interactive": arc.get("interactive", 0),
        "mission_id": arc.get("mission_id", 0),
        "dtime": arc.get("dtime") or 0,
        "videos": [
            {"filename": v["filename"], "cid": v["cid"], "desc": v.get("desc", ""),
             "title": (part_titles[i] if part_titles else v.get("title", ""))}
            for i, v in enumerate(parts)
        ],
    }
    if arc.get("tid_v2"):
        payload["tid_v2"] = arc["tid_v2"]
    csrf = load_cookies().get("bili_jct", "")
    headers = {"User-Agent": UA, "Referer": "https://member.bilibili.com/"}
    with httpx.Client(cookies=load_cookies(), headers=headers, timeout=30) as client:
        resp = client.post("https://member.bilibili.com/x/vu/web/edit",
                           params={"csrf": csrf, "t": int(time.time() * 1000)}, json=payload)
        resp.raise_for_status()
        body = resp.json()
    if body.get("code") != 0:
        raise ToolError(f"Bilibili edit error {body.get('code')}: {body.get('message')}")
    return {"bvid": bvid, "edited": [k for k, v in
            {"title": title, "desc": desc, "tags": tags, "part_titles": part_titles}.items() if v is not None],
            "hint": "The edit goes back through review; check with video_info."}


@mcp.tool(annotations=READ_ONLY)
def video_info(bvid: str) -> dict[str, Any]:
    """Metadata and review state of one of this account's videos.

    Useful as a template: reuse its tid, tags, copyright and source for a similar upload.
    """
    data = _show(bvid)
    arc = data.get("archive", {})
    return {
        "bvid": arc.get("bvid"),
        "title": arc.get("title"),
        "tid": arc.get("tid"),
        "tags": [t for t in (arc.get("tag") or "").split(",") if t],
        "copyright": arc.get("copyright"),
        "source": arc.get("source"),
        "desc": arc.get("desc"),
        "state": arc.get("state"),
        "state_desc": arc.get("state_desc"),
        "reject_reason": arc.get("reject_reason") or None,
        "private": bool(arc.get("is_only_self")),
        "no_reprint": bool(arc.get("no_reprint")),
        "dtime": datetime.fromtimestamp(arc["dtime"]).isoformat(timespec="minutes") if arc.get("dtime") else None,
        "cover": arc.get("cover"),
        "parts": [
            {"title": v.get("title"), "cid": v.get("cid"), "duration": v.get("duration"),
             "fail_desc": v.get("fail_desc") or None, "reject_reason": v.get("reject_reason") or None}
            for v in data.get("videos") or []
        ],
    }


def _trend_row(item: dict[str, Any], src: str) -> dict[str, Any]:
    stat = item.get("stat", {})
    reason = (item.get("rcmd_reason") or {}).get("content") if isinstance(item.get("rcmd_reason"), dict) else item.get("rcmd_reason")
    return {
        "title": item.get("title"),
        "zone": item.get("tnamev2") or item.get("tname"),
        "views": stat.get("view"),
        "likes": stat.get("like"),
        "pub": datetime.fromtimestamp(item["pubdate"]).strftime("%m-%d") if item.get("pubdate") else None,
        "from": src,
        **({"reason": reason} if reason else {}),
        "bvid": item.get("bvid"),
    }


@mcp.tool(annotations=READ_ONLY)
def trending(sources: list[str] | None = None, popular_pages: int = 2, rid: int = 0) -> dict[str, Any]:
    """Titles of what is hot on Bilibili right now, across all zones, for studying title patterns.

    sources: any of "ranking" (全站排行榜, top 100), "popular" (综合热门, 50 per page),
        "weekly" (latest 每周必看). Default: all three, de-duplicated by bvid.
    popular_pages: pages of 综合热门 to fetch (1-10).
    rid: restrict the ranking to one top-level zone (0 = all zones; see `zones`).
    """
    sources = sources or ["ranking", "popular", "weekly"]
    rows: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {}

    def add(items: list[dict[str, Any]], src: str) -> None:
        for item in items:
            rows.setdefault(item.get("bvid"), _trend_row(item, src))

    if "ranking" in sources:
        add(web_get("https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": rid, "type": "all"}).get("list") or [], "ranking")
    if "popular" in sources:
        for pn in range(1, max(1, min(popular_pages, 10)) + 1):
            data = web_get("https://api.bilibili.com/x/web-interface/popular", {"ps": 50, "pn": pn})
            add(data.get("list") or [], "popular")
            if data.get("no_more"):
                break
    if "weekly" in sources:
        series = web_get("https://api.bilibili.com/x/web-interface/popular/series/list").get("list") or []
        if series:
            latest = series[0]
            meta["weekly"] = latest.get("name")
            add(wbi_get("https://api.bilibili.com/x/web-interface/popular/series/one",
                        {"number": latest["number"]}).get("list") or [], "weekly")
    return {"fetched": datetime.now().isoformat(timespec="minutes"), "count": len(rows), **meta,
            "videos": list(rows.values())}


# Meme templates rarely reach the ranking as one big video; they show up as explainer
# videos ("X是什么梗") and as a flood of fresh uploads reusing the phrase.
_MEME_EXPLAINER_KEYWORDS = ["是什么梗", "梗百科", "梗知识", "梗指南"]
_MEME_PATTERNS = [
    re.compile(r"([^【】\s#？?！!，,。：:]{2,20}?)[”\"」』]?\s*(?:到底)?是(?:什么|啥)梗"),
    re.compile(r"【[^】]*梗(?:知识|百科|指南|人物)】\s*[“\"「『]?([^？?！!，,。#【\s”」』]{2,20})"),
    re.compile(r"梗(?:百科|库|知识|指南)之[：:]\s*[“\"「『]?([^？?！!，,。#\s”」』]{2,20})"),
]
_MEME_EXPLAINER = re.compile(r"是(?:什么|啥)梗|梗(?:百科|知识|指南|科普|库)")
_PLACEHOLDER = re.compile(r"X{2,}|x{2,}|ＸＸ")


_buvid: dict[str, str] = {}


def _search(keyword: str, order: str, days: int | None = None, page: int = 1) -> dict[str, Any]:
    """Video search. Search is risk-controlled: it wants a buvid3 device cookie and paced
    requests, and answers a bare ``v_voucher`` (captcha demand) when it is unhappy."""
    if not _buvid:
        spi = web_get("https://api.bilibili.com/x/frontend/finger/spi")
        _buvid.update(buvid3=spi["b_3"], buvid4=spi["b_4"])
    params: dict[str, Any] = {"search_type": "video", "keyword": keyword, "order": order, "page": page}
    if days:
        now = int(time.time())
        params |= {"pubtime_begin_s": now - days * 86400, "pubtime_end_s": now}
    data = wbi_get("https://api.bilibili.com/x/web-interface/wbi/search/type", params,
                   cookies=_buvid, referer="https://search.bilibili.com/")
    if "v_voucher" in data and "result" not in data:
        raise ToolError("Bilibili search is rate-limited (v_voucher); wait a few minutes and retry.")
    time.sleep(0.8)
    for v in data.get("result") or []:
        v["title"] = re.sub(r"<[^>]+>", "", v.get("title", ""))
    return data


def _search_videos(keyword: str, order: str, days: int | None = None, page: int = 1) -> list[dict[str, Any]]:
    return _search(keyword, order, days, page).get("result") or []


def _meme_phrase(title: str) -> str | None:
    for pat in _MEME_PATTERNS:
        m = pat.search(title)
        if m:
            phrase = re.sub(r"^[【\[].*?[】\]]", "", m.group(1)).strip("“”\"「」『』'‘’ ")
            if len(phrase) >= 2 and not _MEME_EXPLAINER.search(phrase):
                return phrase
    return None


@mcp.tool(annotations=READ_ONLY)
def memes(days: int = 60, check: int = 30, refresh: bool = False) -> dict[str, Any]:
    """Meme templates (定型文/梗) spreading on Bilibili right now, ranked by how many new videos reuse them.

    Candidates come from meme-explainer videos of the last `days` days (是什么梗/梗百科/梗知识/梗指南,
    most-watched first); each is then searched among uploads of the last 48 hours. `uses_48h`
    estimates how many of those titles contain the phrase — the real sign a template is alive
    now, unlike explainer views, which lag. `examples` are the most-played of those reuses: read
    them for the template's actual shape (e.g. "根本没有这样的X，你只是怕了"). `check`: how many
    candidates to test. Search is rate-limited (a run makes about 12 + `check` requests), so
    results are cached for 3 hours; `refresh=True` ignores the cache.
    """
    cache = home() / "memes_cache.json"
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < 3 * 3600:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("params") == [days, check]:
            return {**cached["result"], "cached": True}
    candidates: dict[str, dict[str, Any]] = {}
    for kw in _MEME_EXPLAINER_KEYWORDS:
        for page in (1, 2, 3):
            for v in _search_videos(kw, "click", days, page):
                phrase = _meme_phrase(v["title"])
                if not phrase:
                    continue
                key = _PLACEHOLDER.sub("", phrase).lower()
                c = candidates.setdefault(key, {"meme": phrase, "explainer_views": 0, "explainer": v["title"]})
                c["explainer_views"] += v.get("play") or 0

    results: list[dict[str, Any]] = []
    note = None
    for c in sorted(candidates.values(), key=lambda c: -c["explainer_views"])[:max(1, min(check, 60))]:
        core = _PLACEHOLDER.sub("", c["meme"]).strip("的 ")
        needle = re.sub(r"\s+", "", core).lower()
        if len(needle) < 2:
            continue
        try:
            data = _search(core, "click", days=2)
        except ToolError as e:
            note = f"stopped after {len(results)} candidates: {e}"
            break
        page = data.get("result") or []
        hits = [v for v in page if needle in re.sub(r"\s+", "", v["title"]).lower()
                and not _MEME_EXPLAINER.search(v["title"])]
        # Search matches loosely; scale the 48h total by the share of exact reuses on page 1.
        uses = round((data.get("numResults") or 0) * len(hits) / len(page)) if page else 0
        results.append({**c, "uses_48h": uses,
                        "examples": [f'{v["title"]} ({v.get("play")})' for v in hits[:5]]})
    results.sort(key=lambda r: (-r["uses_48h"], -r["explainer_views"]))
    result = {"fetched": datetime.now().isoformat(timespec="minutes"), **({"note": note} if note else {}),
              "memes": results}
    if not note:
        cache.write_text(json.dumps({"params": [days, check], "result": result}, ensure_ascii=False),
                         encoding="utf-8")
    return result


@mcp.tool(annotations=READ_ONLY)
def zones() -> list[dict[str, Any]]:
    """List upload zones (分区) with their tid, for `upload_video`."""
    data = web_get("https://member.bilibili.com/x/vupre/web/archive/pre", {"lang": "cn"})
    return [
        {"tid": t["id"], "name": t["name"],
         "children": [{"tid": c["id"], "name": c["name"], "desc": c.get("desc", "")}
                      for c in t.get("children") or []]}
        for t in data.get("typelist") or []
    ]


@mcp.tool(annotations=READ_ONLY)
def my_videos(status: str = "all", page: int = 1, page_size: int = 10) -> dict[str, Any]:
    """List this account's videos with review state, newest first.

    status: all | pubing (in review / processing) | pubed (public) | not_pubed (rejected / locked).
    """
    status_map = {"all": "is_pubing,pubed,not_pubed", "pubing": "is_pubing",
                  "pubed": "pubed", "not_pubed": "not_pubed"}
    if status not in status_map:
        raise ToolError(f"status must be one of {sorted(status_map)}")
    data = web_get("https://member.bilibili.com/x/web/archives",
                   {"status": status_map[status], "pn": page, "ps": page_size})
    items = []
    for item in data.get("arc_audits") or []:
        arc = item.get("Archive", {})
        stat = item.get("stat", {})
        items.append({
            "bvid": arc.get("bvid"),
            "title": arc.get("title"),
            "state": arc.get("state"),
            "state_desc": arc.get("state_desc"),
            "reject_reason": arc.get("reject_reason") or None,
            "ptime": datetime.fromtimestamp(arc["ptime"]).isoformat(timespec="minutes") if arc.get("ptime") else None,
            "views": stat.get("view"),
            "likes": stat.get("like"),
            "url": f"https://www.bilibili.com/video/{arc.get('bvid')}",
        })
    return {"total": data.get("page", {}).get("count"), "videos": items}


# --------------------------------------------------------- feedback


def feedback_store() -> feedback.Store:
    return feedback.Store(home() / "feedback.json")


@mcp.tool(annotations=READ_ONLY)
def feedback_report(days: int = 90) -> dict[str, Any]:
    """How posted videos performed, for learning which titles work.

    Refreshes the counters first (the daily job also does this hourly). Each video posted in the
    last `days` days shows views at 24h / 72h / 7d / 30d, `vs_median` (views at its latest
    milestone / channel median at that milestone), `engagement` ((like+coin+fav)/view), and its
    title `style` / `meme` as recorded by `feedback_tag`; `groups` gives medians per style and
    meme vs no meme. Small groups (n < 3) are anecdotes, not evidence.
    """
    feedback.collect(feedback_store(), web_get)
    return feedback.report(feedback_store(), days)


@mcp.tool()
def feedback_tag(bvid: str, style: str, meme: str = "", note: str = "") -> dict[str, Any]:
    """Record how a video was titled, right after uploading it, so `feedback_report` can relate
    title choices to results.

    style: the title's main structure, one of 冒号体 (`人名：看点`), 钩子 (Chinese narrative hook),
        白描 (plain quiet description), 惯用格式 (`【现代爵士/地区】曲名 - 艺人`), 定型文 (structured
        template such as the 延毕 pattern), 烂梗 (internet catchphrase meme), 自拍现场.
    meme: the template/meme borrowed, in its original form (empty if none).
    note: anything else worth correlating later (e.g. "日本艺人", "J-Pop", "首次搬运该乐手").
    """
    if style not in feedback.STYLES:
        raise ToolError(f"style must be one of {list(feedback.STYLES)}")
    return feedback.tag(feedback_store(), bvid, style, meme, note)


# --------------------------------------------------------- queue / subscriptions


def store() -> upload_queue.Store:
    return upload_queue.Store(home() / "queue.json")


@mcp.tool()
def queue_add(url: str, note: str = "") -> dict[str, Any]:
    """Add a YouTube video to the upload queue (the daily job posts queued videos only
    Fri (2), Sat (2) and Sun (1), from the evening on). With YouTube logged in it is also
    added to the user's queue playlist (see `queue_playlist`)."""
    return upload_queue.add(store(), url, origin="manual", note=note)


@mcp.tool(annotations=READ_ONLY)
def queue_list(status: str = "open") -> dict[str, Any]:
    """Show the upload queue.

    status: open (pending + processing + failed) | pending | done | failed | skipped | all.
    """
    data = store().load()
    open_states = {"pending", "processing", "failed"}
    items = [i for i in data["items"]
             if status == "all" or (status == "open" and i["status"] in open_states) or i["status"] == status]
    return {"playlist": data.get("playlist"),
            "auto_posts_today": upload_queue.auto_posts_today(data),
            "daily_auto_limit": upload_queue.daily_auto_limit(),
            "weekly_auto_limits": dict(zip(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
                                           upload_queue.WEEKLY_AUTO_LIMITS)),
            "items": items}


@mcp.tool()
def queue_claim(auto: bool, item_id: str = "") -> dict[str, Any]:
    """Take one queue item to post now and mark it processing.

    auto=True: the scheduled daily job. Refused once today's auto limit is used.
    auto=False: the user explicitly asked to post (e.g. "投"); does not count toward the limit.
    item_id: a specific item (also retries a failed one); empty takes the oldest pending.
    Afterwards always call `queue_finish`.
    """
    return upload_queue.claim_next(store(), auto=auto, item_id=item_id)


@mcp.tool()
def queue_finish(item_id: str, status: str, bvid: str = "", note: str = "") -> dict[str, Any]:
    """Record the outcome of a claimed item.

    status: done (uploaded; give bvid) | failed (can be retried) | skipped (not worth posting:
    short, not music, already on Bilibili, ...; say why in note) | pending (put it back).
    A done video from the queue playlist is moved to the "B站已投" playlist.
    """
    return upload_queue.finish(store(), item_id, status, bvid, note)


@mcp.tool()
def queue_remove(item_id: str) -> dict[str, Any]:
    """Delete an item from the queue."""
    return upload_queue.remove(store(), item_id)


@mcp.tool()
async def youtube_login() -> dict[str, Any]:
    """Authorize access to the user's YouTube playlists (opens Google consent in the browser).

    Needs the Desktop-app OAuth client JSON at ~/.bilibili-mcp/youtube_client.json.
    Afterwards the queue playlist is read via the API, `queue_add` also adds to it, and
    posted videos are moved to the "B站已投" playlist.
    """
    from bilibili_mcp.youtube import YouTube

    return await asyncio.to_thread(YouTube(home()).login)


@mcp.tool()
def queue_playlist(playlist: str = "") -> dict[str, Any]:
    """Sync the queue with the user's YouTube queue playlist (public or unlisted).

    playlist: a playlist URL or id to (re)link it; empty just syncs the linked one.
    New videos in the playlist are queued, its order is the posting order, and pending
    items removed from it leave the queue. The daily job syncs every hour.
    """
    if playlist:
        return upload_queue.set_playlist(store(), playlist)
    return upload_queue.sync_playlist(store())


@mcp.tool()
def subscribe(channel: str) -> dict[str, Any]:
    """Follow a YouTube channel (URL, @handle or UC... id). Only videos published after
    subscribing are queued."""
    return upload_queue.subscribe(store(), channel)


@mcp.tool()
def unsubscribe(channel: str) -> dict[str, Any]:
    """Stop following a channel (by name, @handle or channel id)."""
    return upload_queue.unsubscribe(store(), channel)


@mcp.tool(annotations=READ_ONLY)
def subscriptions() -> list[dict[str, Any]]:
    """List followed YouTube channels."""
    return [{k: v for k, v in s.items() if k != "seen"} for s in store().load()["subscriptions"]]


@mcp.tool()
def check_subscriptions() -> dict[str, Any]:
    """Poll followed channels now and queue their new videos (the daily job does this too)."""
    return upload_queue.check_subscriptions(store())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
