# bilibili-mcp — development rules

Read README.md first (architecture, gotchas).

## Workflow for any change

1. Test against the real account, read-only tools first (`account`, `my_videos`,
   `video_info`, `zones`). A real upload publishes to the user's channel: only with
   the user's OK, with `private=True`, and ask them to delete it in the creator
   center afterwards (biliup cannot delete).
2. Privacy check before every commit: no local paths, account names, mids,
   cookies or emails in tracked files. Cookies live in `~/.bilibili-mcp/`, never in the repo.
3. Commit + push, then tell the user to run `/mcp` to reconnect.

## Scope

- biliup does the uploading; add logic here only for what it lacks
  (job tracking, structured views, validation).
- Before upgrading biliup, check PyPI has a Windows wheel for the new version.
