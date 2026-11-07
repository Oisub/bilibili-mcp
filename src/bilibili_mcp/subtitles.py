"""Chinese subtitles for talk-heavy reposts (interviews, masterclasses).

The flow is: `captions` fetches the original YouTube captions (uploaded ones
when the video has them, else YouTube's automatic ones) as a compact cue list;
Claude translates it into a Chinese SRT itself; `burn_command` builds the
ffmpeg command that renders that SRT onto the video (硬字幕), because a
repost's viewers watch on the app where CC tracks are easy to miss, and the
Bilibili CC upload needs the transcoded part's cid and a second review.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

SRT_TIME = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
SRT_CUE_RE = re.compile(SRT_TIME + r"\s*-->\s*" + SRT_TIME)

# Rendered with libass. Sizes are in the script's 288-line PlayRes, so they scale
# with the video: FontSize 15 is about 5% of the frame height.
FONT_DIR = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
STYLE = ("FontName=Microsoft YaHei,FontSize=15,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
         "BorderStyle=1,Outline=1.2,Shadow=0.6,MarginV=14,Bold=0")


class CaptionError(Exception):
    pass


def _ytdlp_env() -> dict[str, str]:
    # uv's venv launcher sets PYTHONHOME, which would point yt-dlp's own Python at ours.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "UV_INTERNAL__PYTHONHOME", "VIRTUAL_ENV")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _video_info(url: str) -> dict[str, Any]:
    exe = os.environ.get("BILIBILI_MCP_YTDLP") or shutil.which("yt-dlp")
    if not exe:
        raise CaptionError("yt-dlp not found on PATH (set BILIBILI_MCP_YTDLP)")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(  # noqa: S603
        [exe, "--no-update", "--no-playlist", "-J", url], capture_output=True, timeout=180,
        creationflags=flags, stdin=subprocess.DEVNULL, env=_ytdlp_env(),
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CaptionError(f"yt-dlp could not read {url}: {err[-500:]}")
    return json.loads(proc.stdout.decode("utf-8"))


def _pick_track(info: dict[str, Any], lang: str) -> tuple[str, str, str]:
    """(kind, language code, json3 url): uploaded captions first, then automatic ones."""
    lang = lang or info.get("language") or "en"
    base = lang.split("-")[0]
    for kind, tracks in (("uploaded", info.get("subtitles") or {}),
                         ("automatic", info.get("automatic_captions") or {})):
        # Exact language, then its regional variants; for automatic captions the
        # "-orig" track is the speech recognition itself, the rest are translations.
        codes = [c for c in tracks if c in (lang, f"{base}-orig")] + \
                [c for c in tracks if c.split("-")[0] == base and c not in (lang, f"{base}-orig")]
        if kind == "automatic":
            codes.sort(key=lambda c: 0 if c.endswith("-orig") else 1)
        for code in codes:
            for fmt in tracks[code]:
                if fmt.get("ext") == "json3" and fmt.get("url"):
                    return kind, code, fmt["url"]
    raise CaptionError(f"the video has no {base} captions on YouTube (uploaded or automatic)")


def _cues_from_json3(data: dict[str, Any]) -> list[tuple[int, int, str]]:
    cues = []
    for ev in data.get("events") or []:
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
        if not text or "tStartMs" not in ev:
            continue
        start = int(ev["tStartMs"])
        cues.append((start, start + int(ev.get("dDurationMs", 0)), re.sub(r"\s+", " ", text)))
    # Automatic captions overlap (each line stays up while the next one types in):
    # end every cue where the next one starts.
    for i in range(len(cues) - 1):
        s, e, t = cues[i]
        cues[i] = (s, min(e, cues[i + 1][0]) if cues[i + 1][0] > s else e, t)
    return cues


def ms_to_srt(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def captions(url: str, lang: str, folder: Path) -> dict[str, Any]:
    """Fetch the video's captions and write them as a cue list for translation."""
    info = _video_info(url)
    kind, code, track_url = _pick_track(info, lang)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(track_url)
        resp.raise_for_status()
        cues = _cues_from_json3(resp.json())
    if not cues:
        raise CaptionError(f"the {code} caption track is empty")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{info['id']}.{code}.cues.txt"
    lines = [f"{ms_to_srt(s)} --> {ms_to_srt(e)} | {t}" for s, e, t in cues]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    srt = folder / f"{info['id']}.zh.srt"
    return {"video_id": info["id"], "kind": kind, "lang": code, "cues": len(cues),
            "duration": info.get("duration"), "path": str(path),
            "srt_path": str(srt), "srt_exists": srt.is_file()}


def parse_srt(text: str) -> list[tuple[int, int, str]]:
    """Cues of an SRT file; raises CaptionError on a malformed or empty file."""
    def ms(g: tuple[str, ...]) -> int:
        h, m, s, f = g
        return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(f.ljust(3, "0"))

    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if SRT_CUE_RE.search(ln)), None)
        if idx is None:
            raise CaptionError(f"SRT block without a time line: {block[:80]!r}")
        m = SRT_CUE_RE.search(lines[idx])
        start, end = ms(m.groups()[:4]), ms(m.groups()[4:])
        body = "\n".join(lines[idx + 1:]).strip()
        if end <= start:
            raise CaptionError(f"SRT cue ends before it starts: {lines[idx]}")
        if body:
            cues.append((start, end, body))
    if not cues:
        raise CaptionError("the SRT file has no cues")
    return cues


def burn_command(video: Path, srt: Path, out: Path) -> list[str]:
    """ffmpeg command rendering `srt` onto `video`. Run it with cwd=srt.parent: the
    subtitles filter takes a file name, and Windows drive colons break its syntax."""
    exe = os.environ.get("BILIBILI_MCP_FFMPEG") or shutil.which("ffmpeg")
    if not exe:
        raise CaptionError("ffmpeg not found on PATH (set BILIBILI_MCP_FFMPEG)")
    fonts = f":fontsdir='{FONT_DIR.as_posix().replace(':', chr(92) + ':')}'" if FONT_DIR.is_dir() else ""
    vf = f"subtitles='{srt.name}'{fonts}:force_style='{STYLE}'"
    return [exe, "-hide_banner", "-nostdin", "-y", "-i", str(video), "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(out)]
