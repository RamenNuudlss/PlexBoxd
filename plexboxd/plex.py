"""Plex side: sign-in (PIN flow), your server's watch history, ratings, and your written reviews."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from . import __version__

log = logging.getLogger("plexboxd.plex")

PRODUCT = "Plexboxd"
PLEX_TV = "https://plex.tv"
CLIENTS_PLEX_TV = "https://clients.plex.tv"
COMMUNITY_API = "https://community.plex.tv/api"

# Plex user reviews are stored on Plex's servers (not your Plex server) and are read
# through Plex's community GraphQL API. This API is not officially documented, so the
# query is loaded from a file you can override at /data/plex_review_query.graphql if
# Plex changes it (see README, "If reviews stop syncing").
DEFAULT_REVIEW_QUERY = """
query PlexboxdReviews($metadataID: ID!, $first: PaginationInt!) {
  activityFeed(first: $first, metadataID: $metadataID, types: [REVIEW]) {
    nodes {
      __typename
      date
      ... on ActivityReview {
        reviewRating: rating
        hasSpoilers
        message
        updatedAt
        status
      }
      userV2 {
        id
        username
      }
    }
  }
}
""".strip()


class PlexError(Exception):
    pass


@dataclass
class PlexReview:
    message: str
    has_spoilers: bool
    rating: float | None      # Plex review rating, 0-10 scale (10 = 5 stars)
    updated_at: str | None


def parse_guids(meta: dict) -> dict[str, str | None]:
    """Pull tmdb/imdb ids and the Plex metadata id out of a PMS metadata item."""
    out: dict[str, str | None] = {"tmdb": None, "imdb": None, "plex": None}
    for g in meta.get("Guid", []) or []:
        gid = g.get("id", "")
        if gid.startswith("tmdb://"):
            out["tmdb"] = gid[len("tmdb://"):]
        elif gid.startswith("imdb://"):
            out["imdb"] = gid[len("imdb://"):]
    guid = meta.get("guid") or ""
    if guid.startswith("plex://movie/"):
        out["plex"] = guid[len("plex://movie/"):]
    # Legacy agent guids, e.g. com.plexapp.agents.imdb://tt0111161?lang=en
    if not out["imdb"] and "agents.imdb://" in guid:
        out["imdb"] = guid.split("agents.imdb://", 1)[1].split("?", 1)[0]
    if not out["tmdb"] and "agents.themoviedb://" in guid:
        out["tmdb"] = guid.split("agents.themoviedb://", 1)[1].split("?", 1)[0]
    return out


def plex_rating_to_letterboxd(value: Any) -> int | None:
    """Plex stores ratings 0-10 (half-star steps). Letterboxd's diary form also uses 0-10
    half-star units (9 = 4.5 stars), so it maps 1:1; round to the nearest half star.
    Letterboxd's lowest rating is half a star, so 0 means 'no rating'."""
    if value in (None, ""):
        return None
    try:
        n = round(float(value))
    except (TypeError, ValueError):
        return None
    n = max(0, min(10, n))
    return n or None


class PlexClient:
    def __init__(self, client_id: str | None = None, token: str | None = None,
                 server_url: str | None = None, server_token: str | None = None,
                 data_dir: str | Path = "/data", timeout: float = 20.0):
        self.client_id = client_id or str(uuid.uuid4())
        self.token = token
        self.server_url = server_url.rstrip("/") if server_url else None
        self.server_token = server_token or token
        self.data_dir = Path(data_dir)
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    def _headers(self, token: str | None = None) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": __version__,
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Device-Name": "Plexboxd",
            "X-Plex-Platform": "Docker",
        }
        if token:
            h["X-Plex-Token"] = token
        return h

    # ---- sign-in (official Plex PIN flow) ---------------------------------
    async def create_pin(self) -> dict:
        r = await self._http.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"}, headers=self._headers())
        r.raise_for_status()
        return r.json()  # {"id": ..., "code": ...}

    def auth_url(self, code: str, forward_url: str) -> str:
        params = {
            "clientID": self.client_id,
            "code": code,
            "forwardUrl": forward_url,
            "context[device][product]": PRODUCT,
        }
        return "https://app.plex.tv/auth#?" + urlencode(params)

    async def check_pin(self, pin_id: int | str) -> str | None:
        r = await self._http.get(f"{PLEX_TV}/api/v2/pins/{pin_id}", headers=self._headers())
        r.raise_for_status()
        return r.json().get("authToken") or None

    async def account(self) -> dict:
        r = await self._http.get(f"{PLEX_TV}/api/v2/user", headers=self._headers(self.token))
        if r.status_code == 401:
            raise PlexError("Plex sign-in expired. Sign in to Plex again.")
        r.raise_for_status()
        return r.json()

    async def servers(self) -> list[dict]:
        """Servers you own, with their connection addresses (local ones first)."""
        r = await self._http.get(
            f"{CLIENTS_PLEX_TV}/api/v2/resources",
            params={"includeHttps": 1, "includeRelay": 0},
            headers=self._headers(self.token),
        )
        r.raise_for_status()
        out = []
        for res in r.json():
            if "server" not in (res.get("provides") or "") or not res.get("owned"):
                continue
            conns = sorted(res.get("connections") or [], key=lambda c: (not c.get("local"), c.get("relay", False)))
            out.append({
                "name": res.get("name"),
                "token": res.get("accessToken") or self.token,
                "connections": [{"uri": c.get("uri"), "local": c.get("local", False)} for c in conns],
            })
        return out

    # ---- your Plex server ----------------------------------------------------
    async def _pms(self, path: str, params: dict | None = None) -> dict:
        if not self.server_url:
            raise PlexError("No Plex server chosen yet.")
        try:
            r = await self._http.get(f"{self.server_url}{path}", params=params, headers=self._headers(self.server_token))
        except httpx.HTTPError as e:
            raise PlexError(f"Can't reach your Plex server at {self.server_url}: {e}") from e
        if r.status_code == 401:
            raise PlexError("Your Plex server rejected the sign-in token. Sign in to Plex again.")
        r.raise_for_status()
        return r.json().get("MediaContainer", {})

    async def test_server(self) -> dict:
        mc = await self._pms("/identity")
        return {"machineIdentifier": mc.get("machineIdentifier"), "version": mc.get("version")}

    async def movie_history(self, since: int, account_id: int | None = None, page_size: int = 200,
                            max_pages: int = 25) -> list[dict]:
        """Movie plays on your server viewed at or after `since` (unix seconds), newest first."""
        results: list[dict] = []
        start = 0
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "sort": "viewedAt:desc",
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": page_size,
            }
            if account_id is not None:
                params["accountID"] = account_id
            mc = await self._pms("/status/sessions/history/all", params)
            items = mc.get("Metadata", []) or []
            if not items:
                break
            reached_older = False
            for it in items:
                viewed = int(it.get("viewedAt") or 0)
                if viewed < since:
                    reached_older = True
                    break
                if it.get("type") == "movie":
                    results.append(it)
            if reached_older or len(items) < page_size:
                break
            start += page_size
        return results

    async def item_history(self, rating_key: str, account_id: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"metadataItemID": rating_key, "sort": "viewedAt:desc"}
        if account_id is not None:
            params["accountID"] = account_id
        mc = await self._pms("/status/sessions/history/all", params)
        return mc.get("Metadata", []) or []

    async def item(self, rating_key: str) -> dict | None:
        try:
            mc = await self._pms(f"/library/metadata/{rating_key}", {"includeGuids": 1})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None  # removed from your library since it was watched
            raise
        items = mc.get("Metadata", []) or []
        return items[0] if items else None

    # ---- your Plex reviews (Plex's community API) -------------------------------
    def review_query(self) -> str:
        override = self.data_dir / "plex_review_query.graphql"
        if override.exists():
            return override.read_text(encoding="utf-8").strip()
        return DEFAULT_REVIEW_QUERY

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        r = await self._http.post(
            COMMUNITY_API,
            json={"query": query, "variables": variables or {}},
            headers={**self._headers(self.token), "Content-Type": "application/json"},
        )
        if r.status_code == 401:
            raise PlexError("Plex sign-in expired. Sign in to Plex again.")
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
            raise PlexError(f"Plex reviews API error: {msgs}")
        return data.get("data") or {}

    async def my_review(self, plex_metadata_id: str, my_user_id: str | None,
                        my_username: str | None) -> PlexReview | None:
        """Your own written review for a movie, or None if you haven't written one."""
        data = await self._graphql(self.review_query(), {"metadataID": plex_metadata_id, "first": 50})
        nodes = _find_review_nodes(data)
        mine = []
        for n in nodes:
            user = n.get("userV2") or n.get("user") or {}
            if (my_user_id and str(user.get("id")) == str(my_user_id)) or \
               (my_username and str(user.get("username", "")).lower() == my_username.lower()):
                mine.append(n)
        if not mine:
            return None
        mine.sort(key=lambda n: n.get("updatedAt") or n.get("date") or "", reverse=True)
        n = mine[0]
        message = (n.get("message") or "").strip()
        rating = n.get("reviewRating", n.get("rating"))
        if not message and rating is None:
            return None
        return PlexReview(
            message=message,
            has_spoilers=bool(n.get("hasSpoilers")),
            rating=float(rating) if isinstance(rating, (int, float)) else None,
            updated_at=n.get("updatedAt") or n.get("date"),
        )

    async def introspect_review_fields(self) -> list[str]:
        """Diagnostics: list query fields that look review/activity related, if Plex allows it."""
        data = await self._graphql("{ __schema { queryType { fields { name args { name } } } } }")
        fields = (((data.get("__schema") or {}).get("queryType") or {}).get("fields")) or []
        hits = []
        for f in fields:
            name = f.get("name", "")
            if any(w in name.lower() for w in ("review", "activit", "rating")):
                args = ", ".join(a["name"] for a in f.get("args") or [])
                hits.append(f"{name}({args})")
        return hits


def _find_review_nodes(data: Any) -> list[dict]:
    """Walk a GraphQL response and collect anything that looks like a review node.
    Tolerates a different query shape if you override the query file."""
    found: list[dict] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "message" in x and ("userV2" in x or "user" in x) and \
               x.get("__typename", "ActivityReview") in ("ActivityReview", "Review", "UserReview"):
                found.append(x)
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return found
