"""GitHub REST client (token via env or `gh auth token`) with on-disk caching."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

API = "https://api.github.com"


def github_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


class GitHub:
    def __init__(self, cache_dir: str | Path | None = None, token: str | None = None):
        self.token = token if token is not None else github_token()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def get(self, path: str, params: dict[str, Any] | None = None, cache: bool = True) -> Any:
        key = f"GET {path} {json.dumps(params or {}, sort_keys=True)}"
        cp = self._cache_path(key) if cache else None
        if cp and cp.exists():
            return json.loads(cp.read_text())
        data = self._request("GET", path, params=params)
        if cp:
            cp.write_text(json.dumps(data))
        return data

    def get_paginated(self, path: str, params: dict[str, Any] | None = None, cache: bool = True,
                      max_pages: int = 100) -> list[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        key = f"GETP {path} {json.dumps(params, sort_keys=True)}"
        cp = self._cache_path(key) if cache else None
        if cp and cp.exists():
            return json.loads(cp.read_text())
        out: list[Any] = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            chunk = self._request("GET", path, params=params)
            if not isinstance(chunk, list):
                raise RuntimeError(f"expected list from {path}, got {type(chunk)}")
            out.extend(chunk)
            if len(chunk) < params["per_page"]:
                break
        if cp:
            cp.write_text(json.dumps(out))
        return out

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, body=body)

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("PATCH", path, body=body)

    def get_text(self, url: str, accept: str, cache: bool = True) -> str:
        """Fetch raw text (e.g. a PR diff via the diff media type)."""
        key = f"GETT {url} {accept}"
        cp = self._cache_path(key) if cache else None
        if cp and cp.exists():
            return cp.read_text()
        headers = self._headers()
        headers["Accept"] = accept
        text = self._request_raw("GET", url, headers=headers)
        if cp:
            cp.write_text(text)
        return text

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
        for attempt in range(6):
            with httpx.Client(timeout=60) as client:
                resp = client.request(method, url, headers=self._headers(), params=params, json=body)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset = int(resp.headers.get("x-ratelimit-reset", "0"))
                wait = max(5, min(reset - int(time.time()) + 2, 120))
                time.sleep(wait)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2**attempt * 2, 30))
                continue
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise RuntimeError(f"GitHub request failed after retries: {method} {url}")

    def _request_raw(self, method: str, url: str, headers: dict[str, str]) -> str:
        if not url.startswith("http"):
            url = f"{API}/{url.lstrip('/')}"
        for attempt in range(6):
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                resp = client.request(method, url, headers=headers)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2**attempt * 2, 30))
                continue
            resp.raise_for_status()
            return resp.text
        raise RuntimeError(f"GitHub raw request failed after retries: {url}")


class Repo:
    """Convenience wrapper for one repository."""

    def __init__(self, gh: GitHub, full_name: str):
        self.gh = gh
        self.full = full_name

    def pr(self, number: int) -> dict[str, Any]:
        return self.gh.get(f"repos/{self.full}/pulls/{number}")

    def pr_files(self, number: int) -> list[dict[str, Any]]:
        return self.gh.get_paginated(f"repos/{self.full}/pulls/{number}/files")

    def pr_diff(self, number: int) -> str:
        return self.gh.get_text(
            f"repos/{self.full}/pulls/{number}", accept="application/vnd.github.v3.diff"
        )

    def pr_review_comments(self, number: int) -> list[dict[str, Any]]:
        return self.gh.get_paginated(f"repos/{self.full}/pulls/{number}/comments")

    def pr_reviews(self, number: int) -> list[dict[str, Any]]:
        return self.gh.get_paginated(f"repos/{self.full}/pulls/{number}/reviews")

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self.gh.get_paginated(f"repos/{self.full}/issues/{number}/comments")

    def pr_commits(self, number: int) -> list[dict[str, Any]]:
        return self.gh.get_paginated(f"repos/{self.full}/pulls/{number}/commits")

    def file_at(self, path: str, ref: str) -> str | None:
        """File content at a ref, or None if missing/binary/too large."""
        try:
            data = self.gh.get(f"repos/{self.full}/contents/{path}", params={"ref": ref})
        except httpx.HTTPStatusError:
            return None
        if isinstance(data, dict) and data.get("encoding") == "base64":
            import base64

            try:
                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return None
        return None

    def tree(self, ref: str) -> list[str]:
        """All file paths at a ref."""
        data = self.gh.get(f"repos/{self.full}/git/trees/{ref}", params={"recursive": "1"})
        return [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]

    def comment_reactions(self, comment_id: int, kind: str = "pulls") -> list[dict[str, Any]]:
        # kind: "pulls" for review comments, "issues" for issue comments
        return self.gh.get_paginated(f"repos/{self.full}/{kind}/comments/{comment_id}/reactions")
