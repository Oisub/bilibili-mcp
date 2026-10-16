# bilibili-mcp

MCP server for uploading videos to Bilibili (B站投稿), built on
[biliup](https://github.com/biliup/biliup).

## Tools

| Tool | What it does |
| --- | --- |
| `account` | Which account is logged in; whether the cookies still work |
| `login` | QR login: opens a QR image on the desktop, waits up to 3 min for the app scan |
| `upload_video` | Upload + submit a new video (one file per part). Runs in the background |
| `append_parts` | Append parts (分P) to an existing video. Runs in the background |
| `upload_status` | Progress / result (`bvid`) of upload jobs, with a log tail |
| `edit_video` | Change title / description / tags / part titles of a submitted video (goes back to review) |
| `my_videos` | This account's videos with review state and rejection reasons |
| `video_info` | Full metadata of one video — reuse its tid / tags / source as a template |
| `zones` | Zone list (分区) with `tid` values |
| `trending` | What's hot site-wide right now (排行榜 + 综合热门 + 每周必看, ~230 titles) for studying title patterns |
| `memes` | Meme templates (梗/定型文) spreading right now: candidates from 梗百科-style explainer videos, ranked by how many uploads of the last 48 h reuse them, with top examples (cached 3 h) |
| `feedback_report` | How posted videos did: views at 24h / 72h / 7d / 30d vs the channel median, grouped by title style and meme use |
| `feedback_tag` | After an upload, record the title's style and borrowed meme so the report can relate titles to results |

### Queue and subscriptions

| Tool | What it does |
| --- | --- |
| `queue_add` / `queue_list` / `queue_remove` | Upload queue of YouTube videos |
| `queue_claim` | Take one item to post (`auto=True` for the daily job, limited per weekday — none Mon–Thu, 2 Fri/Sat, 1 Sun; `auto=False` when the user asks) |
| `queue_finish` | Record the outcome: done (with bvid) / failed / skipped / pending |
| `subscribe` / `unsubscribe` / `subscriptions` | Follow YouTube channels (URL, @handle or channel id) |
| `check_subscriptions` | Poll followed channels (RSS) and queue new videos |
| `queue_playlist` | Link / sync a YouTube playlist that acts as the manual queue |
| `youtube_login` | OAuth consent for the user's YouTube playlists (optional) |

The manual queue can be managed from YouTube: link a public or unlisted playlist with
`queue_playlist(<url>)`. Videos added to it are queued, its order is the posting
order, and pending videos removed from it leave the queue. Posted videos may stay in
the playlist (the queue remembers them, so nothing is posted twice).

With `youtube_login` (optional) the playlist can be private, `queue_add` also adds to
it, and each posted video is moved to a "B站已投" playlist. Setup: in Google Cloud,
create a project, enable *YouTube Data API v3*, configure the OAuth consent screen
(External, then **publish to production** — a Testing app's token expires after 7
days), create an OAuth client of type *Desktop app*, download its JSON to
`~/.bilibili-mcp/youtube_client.json`, then call `youtube_login`.

State is `~/.bilibili-mcp/queue.json`. A subscription only queues videos published
after it was added.

**Scheduled job** — `python -m bilibili_mcp.daily`, run hourly (e.g. Windows Task
Scheduler with `pythonw.exe` so no window opens). It syncs the queue playlist, polls subscriptions and, when
there is something to post, runs a headless `claude -p` that claims, writes the
metadata, uploads and finishes each item:
- videos from subscribed channels are posted as soon as they are found, no limit;
- manually queued videos are posted from `BILIBILI_MCP_POST_HOUR` (default 18), up to a per-weekday limit
  (`WEEKLY_AUTO_LIMITS`: none Monday–Thursday, two on Friday and Saturday, one on Sunday).
Every run also snapshots the counters of posted videos at 24h / 72h / 7d / 30d
(`~/.bilibili-mcp/feedback.json`). About once a week, on a run with nothing to post and
at least 3 newly matured videos, a headless review reads `feedback_report` and may edit
the "数据反馈" section of the style guide within the bounds written there, logging to
`~/.bilibili-mcp/feedback/changes.md` (prompt override: `review_prompt.md`).
The prompt is `~/.bilibili-mcp/daily_prompt.md` if present, else the built-in
default in `daily.py`. Logs go to `~/.bilibili-mcp/daily/<date>.log`;
`--dry-run` decides without starting Claude.

## Install

```
uv sync
claude mcp add bilibili -s user -- "<repo>/.venv/Scripts/bilibili-mcp.exe"
```

A newly added server only shows up after restarting the Claude Code session.

## How it works

- **Login** uses biliup's `stream_gears` bindings (`get_qrcode` / `login_by_qrcode`).
  Cookies are stored in `~/.bilibili-mcp/cookies.json` (override the folder with
  `BILIBILI_MCP_HOME`). Nothing secret passes through the chat.
- **Uploads** run the `biliup` CLI as a background process, because a large file
  takes minutes and the CLI prints the resulting BV id (the Python `upload()`
  binding returns nothing). Each job writes `jobs/<id>.log` and `jobs/<id>.json`.
- **Read-only views** call the creator-center web APIs with the same cookies.
  Endpoints that answer `-352` (risk control) need a WBI signature; `wbi_get`
  adds it (keys from the nav API, rotated daily).

## Gotchas

- biliup is pinned to **1.2.9**: 1.2.10+ publish only an sdist on Windows, which
  needs a Rust/MSVC toolchain to build.
- `publish_at` (定时发布) must be at least 4 hours ahead.
- `copyright=2` (转载) requires `source`.
- Descriptions are cut at **250 characters**. On upload, Bilibili prepends
  `<source>\n` to a repost's description, and that counts too; `upload_video`
  rejects a desc that would be cut and drops a duplicated leading source link.
  The web edit used by `edit_video` does not prepend the source.
- biliup names each part (分P) after its file stem. `part_titles` works by
  hard-linking each file as `jobs/parts-*/<n>/<title><ext>` for the upload
  (symlink, then copy, if the link fails); the folder is removed when the job
  ends. Characters Windows forbids in file names become full-width (`:` → `：`).
- Bilibili silently drops some tags (e.g. `测试`): a test upload tagged
  `测试,MCP` came back with only `MCP`. Check tags with `video_info` afterwards.
- "done" in `upload_status` means the submission was accepted; the video still
  goes through review. Check `my_videos` for the outcome.
- biliup cannot delete videos; delete test uploads in the creator center.
