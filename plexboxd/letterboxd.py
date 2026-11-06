"""Letterboxd side: the official Letterboxd API, and nothing else.

Plexboxd never scrapes letterboxd.com, drives a browser, or reuses browser cookies.
You sign in on Letterboxd's own page (OAuth 2.0 authorization code flow) and Plexboxd
stores only the access/refresh tokens it is given.

API reference: https://api-docs.letterboxd.com/
Each user needs their own API client ID and secret, granted by Letterboxd.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from . import __version__

log = logging.getLogger("plexboxd.letterboxd")

API = "https://api.letterboxd.com/api/v0"
AUTHORIZE_URL = f"{API}/auth/authorize"
TOKEN_URL = f"{API}/auth/token"
SCOPES = "content:modify oauth:refresh"


class LetterboxdError(Exception):
    pass


class NotSignedIn(LetterboxdError):
    """No valid Letterboxd sign-in: sign in with Letterboxd again from the dashboard."""


class NotConfigured(LetterboxdError):
    """LETTERBOXD_CLIENT_ID / LETTERBOXD_CLIENT_SECRET aren't set."""


@dataclass
class Film:
    film_id: str            # Letterboxd LID
    slug: str | None
    url: str
    name: str | None = None
    year: int | None = None


@dataclass
class DiaryEntry:
    entry_id: str | None
    film_id: str | None     # Letterboxd LID
    watched_date: date | None
    rewatch: bool


# ---- pure helpers (unit tested) -------------------------------------------------

def to_letterboxd_rating(rating10: int | None) -> float | None:
    """Plexboxd keeps ratings on Plex's 0-10 half-star scale; the API wants 0.5-5.0."""
    if not rating10:
        return None
    return max(0.5, min(5.0, round(int(rating10)) / 2))


def slug_from_link(link: str | None) -> str | None:
    if not link or "/film/" not in link:
        return None
    return link.rstrip("/").rsplit("/film/", 1)[1].split("/", 1)[0] or None


def film_from_api(item: dict) -> Film:
    link = None
    for lk in item.get("links") or []:
        if lk.get("type") == "letterboxd" and lk.get("url"):
            link = lk["url"]
    link = link or item.get("link")
    slug = slug_from_link(link)
    url = link or (f"https://letterboxd.com/film/{slug}/" if slug else f"https://boxd.it/{item['id']}")
    return Film(film_id=str(item["id"]), slug=slug, url=url, name=item.get("name"), year=item.get("releaseYear"))


def entry_from_api(item: dict) -> DiaryEntry:
    diary = item.get("diaryDetails") or {}
    d = None
    if diary.get("diaryDate"):
        try:
            d = date.fromisoformat(str(diary["diaryDate"])[:10])
        except ValueError:
            d = None
    film = item.get("film") or {}
    return DiaryEntry(entry_id=item.get("id"), film_id=str(film["id"]) if film.get("id") else None,
                      watched_date=d, rewatch=bool(diary.get("rewatch")))


def build_creation_request(*, film_id: str, watched: date, rating10: int | None, review: str | None,
                           spoilers: bool, rewatch: bool) -> dict:
    body: dict[str, Any] = {
        "filmId": film_id,
        "diaryDetails": {"diaryDate": watched.isoformat(), "rewatch": bool(rewatch)},
    }
    rating = to_letterboxd_rating(rating10)
    if rating is not None:
        body["rating"] = rating
    if review:
        body["review"] = {"text": review, "containsSpoilers": bool(spoilers)}
    return body


def build_update_request(*, rating10: int | None, review: str | None, spoilers: bool) -> dict:
    body: dict[str, Any] = {}
    rating = to_letterboxd_rating(rating10)
    if rating is not None:
        body["rating"] = rating
    if review:
        body["review"] = {"text": review, "containsSpoilers": bool(spoilers)}
    return body


# ---- API client ------------------------------------------------------------------------

TokenLoader = Callable[[], dict | None]
TokenSaver = Callable[[dict | None], None]


class LetterboxdClient:
    def __init__(self, client_id: str | None, client_secret: str | None,
                 load_tokens: TokenLoader, save_tokens: TokenSaver, timeout: float = 20.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self._load = load_tokens
        self._save = save_tokens
        self._http = httpx.AsyncClient(
            timeout=timeout, transport=transport,
            headers={"User-Agent": f"Plexboxd/{__version__} (+https://github.com/RamenNuudlss/PlexBoxd)",
                     "Accept": "application/json"})

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _require_config(self) -> None:
        if not self.configured:
            raise NotConfigured("Set LETTERBOXD_CLIENT_ID and LETTERBOXD_CLIENT_SECRET (your own Letterboxd API "
                                "credentials) in your .env file, then restart the container.")

    # ---- sign-in (OAuth 2.0 authorization code) --------------------------------------
    def authorize_url(self, redirect_uri: str, state: str) -> str:
        self._require_config()
        return AUTHORIZE_URL + "?" + urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
        })

    async def _token_request(self, form: dict) -> dict:
        self._require_config()
        form = {**form, "client_id": self.client_id, "client_secret": self.client_secret}
        r = await self._http.post(TOKEN_URL, data=form,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code in (400, 401):
            raise NotSignedIn(f"Letterboxd sign-in was refused ({_error_text(r)}).")
        r.raise_for_status()
        data = r.json()
        tokens = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or form.get("refresh_token"),
            "expires_at": int(time.time()) + int(data.get("expires_in") or 3600),
        }
        self._save(tokens)
        return tokens

    async def exchange_code(self, code: str, redirect_uri: str) -> dict:
        return await self._token_request({"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": redirect_uri})

    async def _refresh(self, tokens: dict) -> dict:
        if not tokens.get("refresh_token"):
            self._save(None)
            raise NotSignedIn("Your Letterboxd sign-in expired. Sign in with Letterboxd again.")
        try:
            return await self._token_request({"grant_type": "refresh_token",
                                              "refresh_token": tokens["refresh_token"]})
        except NotSignedIn:
            self._save(None)
            raise NotSignedIn("Your Letterboxd sign-in expired. Sign in with Letterboxd again.")

    async def _access_token(self) -> str:
        tokens = self._load()
        if not tokens or not tokens.get("access_token"):
            raise NotSignedIn("Not signed in to Letterboxd.")
        if int(tokens.get("expires_at") or 0) - 120 < time.time():
            tokens = await self._refresh(tokens)
        return tokens["access_token"]

    def signed_in(self) -> bool:
        t = self._load()
        return bool(t and t.get("access_token"))

    def sign_out(self) -> None:
        self._save(None)

    # ---- requests ----------------------------------------------------------------------
    async def _call(self, method: str, path: str, *, params: Any = None, json: Any = None,
                    retried: bool = False) -> Any:
        self._require_config()
        token = await self._access_token()
        r = await self._http.request(method, f"{API}{path}", params=params, json=json,
                                     headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401 and not retried:
            tokens = self._load() or {}
            await self._refresh(tokens)
            return await self._call(method, path, params=params, json=json, retried=True)
        if r.status_code == 401:
            raise NotSignedIn("Letterboxd rejected the sign-in. Sign in with Letterboxd again.")
        if r.status_code == 403:
            raise LetterboxdError(f"Letterboxd says this isn't allowed ({_error_text(r)}).")
        if r.status_code == 429:
            raise LetterboxdError("Letterboxd rate limit reached; will retry later.")
        if r.status_code >= 400:
            raise LetterboxdError(f"Letterboxd API error {r.status_code}: {_error_text(r)}")
        if not r.content:
            return None
        return r.json()

    async def me(self) -> dict:
        """{'id': member LID, 'username': ...}"""
        data = await self._call("GET", "/me")
        member = data.get("member") or {}
        return {"id": member.get("id"), "username": member.get("username"),
                "displayName": member.get("displayName")}

    async def resolve_film(self, tmdb_id: str | None, imdb_id: str | None) -> Film | None:
        """Find the Letterboxd film for a TMDB/IMDb id."""
        ids = []
        if tmdb_id:
            ids.append(f"tmdb:{tmdb_id}")
        if imdb_id:
            ids.append(f"imdb:{imdb_id}")
        if not ids:
            return None
        data = await self._call("GET", "/films", params=[("filmId", i) for i in ids] + [("perPage", "10")])
        items = (data or {}).get("items") or []
        return film_from_api(items[0]) if items else None

    async def diary(self, member_id: str, per_page: int = 100) -> list[DiaryEntry]:
        """Your most recent diary entries."""
        data = await self._call("GET", "/log-entries", params={
            "member": member_id, "memberRelationship": "Owner", "where": "HasDiaryDate",
            "sort": "WhenAdded", "perPage": per_page})
        return [entry_from_api(i) for i in (data or {}).get("items") or []]

    async def find_entry(self, member_id: str, film_id: str, watched: date) -> str | None:
        """The id of your diary entry for this film on this date (the create call doesn't return it)."""
        data = await self._call("GET", "/log-entries", params={
            "member": member_id, "memberRelationship": "Owner", "film": film_id,
            "where": "HasDiaryDate", "sort": "WhenAdded", "perPage": 20})
        entries = [entry_from_api(i) for i in (data or {}).get("items") or []]
        for e in entries:
            if e.watched_date == watched:
                return e.entry_id
        return entries[0].entry_id if entries else None

    async def create_entry(self, *, member_id: str, film: Film, watched: date, rating10: int | None,
                           review: str | None, spoilers: bool, rewatch: bool) -> str | None:
        """Log a diary entry. Returns the new entry's id (looked up afterwards) if it can be found."""
        await self._call("POST", "/log-entries", json=build_creation_request(
            film_id=film.film_id, watched=watched, rating10=rating10, review=review,
            spoilers=spoilers, rewatch=rewatch))
        try:
            return await self.find_entry(member_id, film.film_id, watched)
        except LetterboxdError as e:
            log.warning("Logged %s but couldn't look up the new entry id: %s", film.name, e)
            return None

    async def update_entry(self, entry_id: str, *, rating10: int | None, review: str | None,
                           spoilers: bool) -> None:
        body = build_update_request(rating10=rating10, review=review, spoilers=spoilers)
        if body:
            await self._call("PATCH", f"/log-entry/{entry_id}", json=body)


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return (r.text or "")[:200] or f"HTTP {r.status_code}"
    if isinstance(data, dict):
        for key in ("message", "error_description", "error", "messages"):
            if data.get(key):
                return str(data[key])[:300]
    return str(data)[:300]
