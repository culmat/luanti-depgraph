"""A polite client for the ContentDB REST API (https://content.luanti.org/help/api/).

It identifies itself, never exceeds `rate` requests per second, retries on
throttling, and caches per-package detail by release id so repeated builds
only fetch what changed.
"""

import json
import time
from pathlib import Path
from typing import Any

import httpx

from . import __version__

BASE_URL = "https://content.luanti.org"
USER_AGENT = f"luanti-depgraph/{__version__} (+https://github.com/culmat/luanti-depgraph)"


class ContentDB:
    def __init__(self, base_url: str = BASE_URL, cache_dir: Path | None = None,
                 rate: float = 2.0, timeout: float = 60.0):
        self.client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
        self.min_interval = 1.0 / rate
        self.cache_dir = cache_dir
        self.requests = 0
        self._last = 0.0

    def get(self, path: str, params: dict | None = None) -> Any:
        for attempt in range(5):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            response = self.client.get(path, params=params)
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * 2**attempt)
                continue
            response.raise_for_status()
            return response.json()
        response.raise_for_status()

    def packages(self) -> list[dict]:
        """All approved packages, including their repo URL (one request)."""
        return self.get("/api/packages/", {"fmt": "vcs"})

    def dependencies(self) -> list[dict]:
        """provides/depends/optional_depends for every approved package (~12 requests).

        Paged by creation date: the default order is by score, which changes with every
        download while we page, so packages could move between pages and be missed."""
        items, page = [], 1
        while True:
            data = self.get("/api/dependencies/",
                            {"n": 300, "page": page, "sort": "created_at", "order": "asc"})
            items.extend(data["items"])
            if page >= data["page_count"]:
                return items
            page += 1

    def scores(self) -> list[dict]:
        return self.get("/api/scores/")

    def updates(self) -> dict[str, int]:
        """Map of "author/name" (aliases included) to the latest release id."""
        return self.get("/api/updates/")

    def package_details(self, author: str, name: str, release: int | None,
                        cached_only: bool = False) -> dict | None:
        """Full package JSON plus its latest release, cached by release id.
        With cached_only, return None instead of fetching."""
        cache_file = None
        if self.cache_dir is not None:
            cache_file = self.cache_dir / "packages" / author / f"{name}.json"
            if cache_file.exists():
                cached = json.loads(cache_file.read_text())
                if cached.get("release") == release:
                    return cached
        if cached_only:
            return None
        detail = self.get(f"/api/packages/{author}/{name}/")
        releases = self.get(f"/api/packages/{author}/{name}/releases/")
        detail["latest_release"] = releases[0] if releases else None
        if cache_file is not None:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(detail))
        return detail
