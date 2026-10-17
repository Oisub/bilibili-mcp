"""Performance feedback for posted videos.

The daily job calls ``collect`` on every (daily) run: one creator-center request per 50
videos returns every video's counters, and each video gets a snapshot when it
passes a milestone age (MILESTONES), so videos can be compared at the same age.
Videos already older than the tracking window when first seen (the back
catalogue) get only a ``total`` snapshot; they form the channel baseline.

After an upload the agent records *how* it titled the video (``tag``: title
style, meme used), so ``report`` can relate title choices to results. A weekly
review (daily.py) feeds that report to a headless Claude, which may adjust the
feedback section of the channel style guide within the bounds written there.

State lives in ``<home>/feedback.json``:

    {"videos": {bvid: {"title", "ptime", "copyright", "style", "meme", "note",
                       "snapshots": {"24h": {...counters, "age_h"}, ...}}},
     "last_review": iso | null}
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

MILESTONES = {"24h": 24, "72h": 72, "7d": 168, "30d": 720}
TRACK_DAYS = 35  # newer videos are tracked per milestone; older ones are baseline only
COUNTERS = ("view", "like", "coin", "favorite", "share", "reply", "danmaku")
STYLES = ("冒号体", "钩子", "白描", "惯用格式", "定型文", "烂梗", "自拍现场")
REVIEW_EVERY = timedelta(days=7)
REVIEW_MIN_NEW = 3  # videos that reached 7d since the last review

Getter = Callable[..., Any]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"videos": {}, "last_review": None}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def _counters(stat: dict[str, Any], age_h: float) -> dict[str, Any]:
    return {**{k: stat.get(k) or 0 for k in COUNTERS}, "age_h": round(age_h)}


def collect(store: Store, web_get: Getter, now: datetime | None = None) -> dict[str, int]:
    """Record milestone snapshots for this account's public videos. Cheap: one request per 50 videos
    while they are inside the tracking window (the whole catalogue on the first run)."""
    now = now or datetime.now()
    data = store.load()
    videos = data["videos"]
    first_run = not videos
    snapped = added = 0
    pn = 1
    while True:
        page = web_get("https://member.bilibili.com/x/web/archives",
                       {"status": "pubed", "pn": pn, "ps": 50})
        items = page.get("arc_audits") or []
        oldest = now
        for item in items:
            arc, stat = item.get("Archive", {}), item.get("stat", {})
            bvid, ptime = arc.get("bvid"), arc.get("ptime")
            if not bvid or not ptime:
                continue
            posted = datetime.fromtimestamp(ptime)
            oldest = min(oldest, posted)
            age_h = (now - posted).total_seconds() / 3600
            v = videos.get(bvid)
            if v is None:
                v = videos[bvid] = {"ptime": posted.isoformat(timespec="minutes"),
                                    "copyright": arc.get("copyright"), "snapshots": {}}
                added += 1
                if age_h > TRACK_DAYS * 24:
                    v["snapshots"]["total"] = _counters(stat, age_h)
            v["title"] = arc.get("title")
            if "total" in v["snapshots"]:
                continue
            passed = [name for name, hours in MILESTONES.items() if age_h >= hours]
            for name in passed:
                # A milestone missed by far (the PC was off) would only repeat a later count;
                # keep just the latest passed one then.
                late = age_h > MILESTONES[name] * 1.5 and name != passed[-1]
                if not late and name not in v["snapshots"]:
                    v["snapshots"][name] = _counters(stat, age_h)
                    snapped += 1
        pn += 1
        if not items or len(items) < 50 or (not first_run and now - oldest > timedelta(days=TRACK_DAYS)):
            break
    store.save(data)
    return {"new_videos": added, "snapshots": snapped}


def tag(store: Store, bvid: str, style: str, meme: str = "", note: str = "") -> dict[str, Any]:
    data = store.load()
    v = data["videos"].setdefault(bvid, {"snapshots": {}})
    v.update(style=style, meme=meme or None, note=note or None)
    store.save(data)
    return {"bvid": bvid, **{k: v.get(k) for k in ("style", "meme", "note")}}


def _latest(v: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    for name in (*reversed(MILESTONES), "total"):
        if name in v["snapshots"]:
            return name, v["snapshots"][name]
    return None


def report(store: Store, days: int = 90) -> dict[str, Any]:
    """Recent videos against the channel baseline at the same age, plus medians by title style."""
    data = store.load()
    videos = data["videos"]
    baseline = {}
    for name in (*MILESTONES, "total"):
        views = [v["snapshots"][name]["view"] for v in videos.values() if name in v["snapshots"]]
        if views:
            baseline[name] = {"median_views": statistics.median(views), "n": len(views)}

    cutoff = datetime.now() - timedelta(days=days)
    rows = []
    for bvid, v in videos.items():
        if "total" in v["snapshots"] or not v.get("ptime") or datetime.fromisoformat(v["ptime"]) < cutoff:
            continue
        latest = _latest(v)
        row: dict[str, Any] = {"bvid": bvid, "title": v.get("title"), "posted": v.get("ptime"),
                               "style": v.get("style"), "meme": v.get("meme")}
        if v.get("note"):
            row["note"] = v["note"]
        for name in MILESTONES:
            s = v["snapshots"].get(name)
            if s:
                row[name] = s["view"]
        if latest:
            name, s = latest
            base = baseline.get(name, {}).get("median_views") or 0
            row["vs_median"] = round(s["view"] / base, 2) if base else None
            row["engagement"] = round((s["like"] + s["coin"] + s["favorite"]) / s["view"], 3) if s["view"] else None
        rows.append(row)
    rows.sort(key=lambda r: r["posted"], reverse=True)

    groups: dict[str, list[float]] = {}
    for r in rows:
        if r.get("vs_median") is None:
            continue
        groups.setdefault(f"style:{r['style'] or 'untagged'}", []).append(r["vs_median"])
        groups.setdefault("meme:yes" if r["meme"] else "meme:no", []).append(r["vs_median"])
    by_group = {k: {"n": len(xs), "median_vs_median": round(statistics.median(xs), 2)}
                for k, xs in sorted(groups.items())}
    return {"baseline": baseline, "groups": by_group, "videos": rows,
            "last_review": data.get("last_review"),
            "note": "vs_median = views at the latest milestone / channel median at that milestone. "
                    "Groups with n < 3 are anecdotes, not evidence."}


def review_due(store: Store, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    data = store.load()
    last = datetime.fromisoformat(data["last_review"]) if data.get("last_review") else None
    if last and now - last < REVIEW_EVERY:
        return False
    since = last or datetime.min
    matured = [v for v in data["videos"].values()
               if "7d" in v["snapshots"] and v.get("ptime")
               and datetime.fromisoformat(v["ptime"]) + timedelta(days=7) > since]
    return len(matured) >= REVIEW_MIN_NEW


def mark_reviewed(store: Store, now: datetime | None = None) -> None:
    data = store.load()
    data["last_review"] = (now or datetime.now()).isoformat(timespec="minutes")
    store.save(data)
