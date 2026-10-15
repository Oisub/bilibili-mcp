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
from mcp.types import ToolAnnotations

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
        raise RuntimeError(f"biliup CLI not found next to the Python interpreter ({exe})")
    return str(exe)


# ------------------------------------------------------------------- cookies


def load_cookies() -> dict[str, str]:
    path = cookie_file()
    if not path.exists():
        raise RuntimeError("Not logged in. Call the `login` tool first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    cookies = data.get("cookie_info", {}).get("cookies", [])
    jar = {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    if "SESSDATA" not in jar:
        raise RuntimeError("Cookie file has no SESSDATA. Call `login` again.")
    return jar


def web_get(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET a Bilibili web API with the saved cookies and unwrap ``data``."""
    headers = {"User-Agent": UA, "Referer": "https://member.bilibili.com/"}
    with httpx.Client(cookies=load_cookies(), headers=headers, timeout=20) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Bilibili API error {body.get('code')}: {body.get('message')}")
    return body.get("data")


def whoami() -> dict[str, Any]:
    data = web_get("https://api.bilibili.com/x/web-interface/nav")
    if not data.get("isLogin"):
        raise RuntimeError("Cookies expired. Call `login` again.")
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


def _start_job(kind: str, args: list[str], summary: dict[str, Any]) -> dict[str, Any]:
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
        raise ValueError("title is required")
    if len(title) > 80:
        raise ValueError("title must be 80 characters or fewer")
    if not tags:
        raise ValueError("at least one tag is required")
    if copyright == 2 and not source:
        raise ValueError("source is required for a repost (copyright=2)")
    if cover and not Path(cover).is_file():
        raise ValueError(f"cover image not found: {cover}")
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
            raise ValueError("publish_at must be at least 4 hours from now")
        args += ["--dtime", str(ts)]
    return args


def _check_files(files: list[str]) -> list[str]:
    if not files:
        raise ValueError("files must list at least one video")
    resolved = []
    for f in files:
        p = Path(f)
        if not p.is_file():
            raise ValueError(f"video not found: {f}")
        resolved.append(str(p.resolve()))
    return resolved


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
    """
    paths = _check_files(files)
    args = ["upload", *_metadata_args(title, tid, tags, desc, cover, copyright, source,
                                       publish_at, private, dynamic, no_reprint, tid_v2)]
    if line:
        args += ["--line", line]
    meta = _start_job("upload", [*args, *paths], {"title": title, "files": paths})
    return {"job_id": meta["job_id"], "status": "running",
            "hint": "Call upload_status with this job_id to follow progress."}


@mcp.tool()
def append_parts(bvid: str, files: list[str], line: str = "") -> dict[str, Any]:
    """Append new parts (分P) to an existing video of this account. Runs in the background."""
    paths = _check_files(files)
    args = ["append", "--vid", bvid]
    if line:
        args += ["--line", line]
    meta = _start_job("append", [*args, *paths], {"bvid": bvid, "files": paths})
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
        raise ValueError(f"unknown job_id: {job_id}")
    meta = json.loads(path.read_text(encoding="utf-8"))
    lines = [ln for ln in _read_log(job_id).splitlines() if ln.strip()]
    meta["log_tail"] = lines[-log_lines:]
    return meta


# --------------------------------------------------------------------- views


@mcp.tool(annotations=READ_ONLY)
def video_info(bvid: str) -> dict[str, Any]:
    """Metadata and review state of one of this account's videos.

    Useful as a template: reuse its tid, tags, copyright and source for a similar upload.
    """
    proc = subprocess.run(  # noqa: S603
        [biliup_exe(), "-u", str(cookie_file()), "show", bvid], cwd=home(),
        capture_output=True, timeout=60, env={**os.environ, "NO_COLOR": "1"},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 or "{" not in out:
        err = ANSI_RE.sub("", (proc.stderr or proc.stdout).decode("utf-8", errors="replace"))
        raise RuntimeError(f"biliup show failed: {err.strip()[-500:]}")
    data = json.loads(out[out.index("{"):])
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
        raise ValueError(f"status must be one of {sorted(status_map)}")
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
