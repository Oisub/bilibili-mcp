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
| `my_videos` | This account's videos with review state and rejection reasons |
| `video_info` | Full metadata of one video — reuse its tid / tags / source as a template |
| `zones` | Zone list (分区) with `tid` values |

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

## Gotchas

- biliup is pinned to **1.2.9**: 1.2.10+ publish only an sdist on Windows, which
  needs a Rust/MSVC toolchain to build.
- `publish_at` (定时发布) must be at least 4 hours ahead.
- `copyright=2` (转载) requires `source`.
- "done" in `upload_status` means the submission was accepted; the video still
  goes through review. Check `my_videos` for the outcome.
- biliup cannot delete videos; delete test uploads in the creator center.
