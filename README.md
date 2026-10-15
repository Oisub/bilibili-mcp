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

### Queue and subscriptions

| Tool | What it does |
| --- | --- |
| `queue_add` / `queue_list` / `queue_remove` | Upload queue of YouTube videos |
| `queue_claim` | Take one item to post (`auto=True` for the daily job, limited to one per day; `auto=False` when the user asks) |
| `queue_finish` | Record the outcome: done (with bvid) / failed / skipped / pending |
| `subscribe` / `unsubscribe` / `subscriptions` | Follow YouTube channels (URL, @handle or channel id) |
| `check_subscriptions` | Poll followed channels (RSS) and queue new videos |

State is `~/.bilibili-mcp/queue.json`. A subscription only queues videos published
after it was added.

**Daily job** — `python -m bilibili_mcp.daily` (schedule it, e.g. with Windows Task
Scheduler using `pythonw.exe` so no window opens): polls subscriptions, and if
something is pending and today's auto quota is unused, runs a headless
`claude -p` that claims one item, writes the metadata, uploads and finishes it.
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
