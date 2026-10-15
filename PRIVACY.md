# Privacy

bilibili-mcp is a local tool that a person runs on their own computer for their own
accounts. It has no server and sends nothing to its authors.

- **YouTube (Google) data**: with the user's consent it reads, adds to and moves
  videos between the user's own YouTube playlists (scope
  `https://www.googleapis.com/auth/youtube`). The OAuth token is stored only on the
  user's computer (`~/.bilibili-mcp/youtube_token.json`) and is used only to call the
  YouTube Data API on the user's behalf. Nothing is shared with third parties.
- **Bilibili data**: login cookies are stored only on the user's computer
  (`~/.bilibili-mcp/cookies.json`) and used only to call Bilibili on the user's behalf.

To revoke YouTube access, delete the token file and remove the app at
https://myaccount.google.com/permissions.
