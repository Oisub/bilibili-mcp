"""YouTube Data API access to the user's own playlists (OAuth, installed-app flow).

Only what the queue playlist needs: read it (also when private), add videos to it,
and move posted videos to a second "posted" playlist. Everything else about
YouTube stays read-only via yt-dlp and RSS.

Files in ``<home>``:

* ``youtube_client.json`` - the OAuth client downloaded from Google Cloud
  (Desktop app type), placed there by the user;
* ``youtube_token.json`` - refresh + access token, written by ``login``.

The Google Cloud app must be "In production": a "Testing" app's refresh token
expires after 7 days.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

SCOPE = "https://www.googleapis.com/auth/youtube"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
LOGIN_TIMEOUT = 300


class YouTube:
    def __init__(self, home: Path) -> None:
        self.client_file = home / "youtube_client.json"
        self.token_file = home / "youtube_token.json"

    # ------------------------------------------------------------------ auth

    def _client(self) -> dict[str, str]:
        if not self.client_file.exists():
            raise ToolError(f"no OAuth client: put the Desktop-app client JSON from Google Cloud at {self.client_file}")
        data = json.loads(self.client_file.read_text(encoding="utf-8"))
        return data.get("installed") or data.get("web") or data

    def logged_in(self) -> bool:
        return self.token_file.exists()

    def login(self) -> dict[str, Any]:
        """Browser consent with a loopback redirect and PKCE; saves the refresh token."""
        client = self._client()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result: dict[str, str] = {}
        done = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                if "code" not in query and "error" not in query:
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update(query)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<p>YouTube 授权完成，可以关掉这个页面了。</p>".encode())
                done.set()

            def log_message(self, *args: Any) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        redirect = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = AUTH_URL + "?" + urllib.parse.urlencode({
                "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
                "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
                "code_challenge": challenge, "code_challenge_method": "S256",
            })
            webbrowser.open(url)
            if not done.wait(LOGIN_TIMEOUT):
                raise ToolError("YouTube consent was not completed within 5 minutes. Call it again.")
        finally:
            server.shutdown()
        if result.get("state") != state:
            raise ToolError("OAuth state mismatch; try again")
        if "error" in result:
            raise ToolError(f"Google refused: {result['error']}")
        resp = httpx.post(TOKEN_URL, data={
            "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "code": result["code"], "code_verifier": verifier, "grant_type": "authorization_code",
            "redirect_uri": redirect,
        }, timeout=30)
        if resp.status_code != 200:
            raise ToolError(f"token exchange failed: {resp.text[:300]}")
        self._save_token(resp.json())
        channel = self.get("channels", part="snippet", mine="true").get("items", [{}])[0]
        return {"logged_in": True, "channel": channel.get("snippet", {}).get("title", "")}

    def _save_token(self, tok: dict[str, Any]) -> None:
        old = json.loads(self.token_file.read_text(encoding="utf-8")) if self.token_file.exists() else {}
        tok = {**old, **tok, "expires_at": time.time() + tok.get("expires_in", 3600) - 60}
        self.token_file.write_text(json.dumps(tok, indent=1), encoding="utf-8")

    def _access_token(self) -> str:
        if not self.token_file.exists():
            raise ToolError("YouTube not logged in: call `youtube_login`")
        tok = json.loads(self.token_file.read_text(encoding="utf-8"))
        if tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        client = self._client()
        resp = httpx.post(TOKEN_URL, data={
            "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
        }, timeout=30)
        if resp.status_code != 200:
            raise ToolError(f"YouTube token refresh failed ({resp.text[:200]}): call `youtube_login` again")
        self._save_token(resp.json())
        return resp.json()["access_token"]

    # ------------------------------------------------------------------- api

    def _call(self, method: str, path: str, params: dict[str, Any], body: dict | None = None) -> dict[str, Any]:
        resp = httpx.request(method, f"{API}/{path}", params=params, json=body, timeout=30,
                             headers={"Authorization": f"Bearer {self._access_token()}"})
        if resp.status_code >= 400:
            raise ToolError(f"YouTube API {method} {path}: {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._call("GET", path, params)

    def playlist_items(self, playlist_id: str) -> list[dict[str, str]]:
        """Videos of a playlist in order: video_id, title, item_id (the playlistItem id)."""
        out, token = [], ""
        while True:
            page = self.get("playlistItems", part="snippet", playlistId=playlist_id, maxResults=50,
                            **({"pageToken": token} if token else {}))
            for it in page.get("items", []):
                sn = it["snippet"]
                out.append({"video_id": sn["resourceId"]["videoId"], "title": sn.get("title", ""),
                            "item_id": it["id"]})
            token = page.get("nextPageToken", "")
            if not token:
                return out

    def playlist_title(self, playlist_id: str) -> str:
        items = self.get("playlists", part="snippet", id=playlist_id).get("items", [])
        return items[0]["snippet"]["title"] if items else ""

    def add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        self._call("POST", "playlistItems", {"part": "snippet"}, {"snippet": {
            "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}})

    def remove_from_playlist(self, playlist_id: str, video_id: str) -> int:
        # Scan the whole list: the videoId filter misses items added moments ago.
        removed = 0
        for it in self.playlist_items(playlist_id):
            if it["video_id"] == video_id:
                self._call("DELETE", "playlistItems", {"id": it["item_id"]})
                removed += 1
        return removed

    def create_playlist(self, title: str, privacy: str = "unlisted") -> str:
        res = self._call("POST", "playlists", {"part": "snippet,status"}, {
            "snippet": {"title": title}, "status": {"privacyStatus": privacy}})
        return res["id"]
