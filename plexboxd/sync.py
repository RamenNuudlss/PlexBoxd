"""The sync engine: Plex history -> enrich with rating/review -> Letterboxd diary."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from .db import DB
from .letterboxd import DiaryEntry, Film, LetterboxdClient, LetterboxdError, NotConfigured, NotSignedIn
from .plex import PlexClient, PlexError, parse_guids, plex_rating_to_letterboxd

log = logging.getLogger("plexboxd.sync")

MAX_ATTEMPTS = 5
HISTORY_OVERLAP = 3600          # re-read the last hour of history each poll in case Plex was slow to record a play
LATE_CHECK_EVERY = 30 * 60      # recheck a logged entry for a newly written review at most this often
DUPLICATE_WINDOW_DAYS = 1       # an existing diary entry within a day of the watch counts as "already logged"


def watch_date(viewed_at: int) -> date:
    """The day you watched it, in the container's time zone (set TZ in docker-compose)."""
    return datetime.fromtimestamp(viewed_at).date()


def is_duplicate(feed: list[DiaryEntry], film_id: str | None, watched: date) -> bool:
    if not film_id:
        return False
    for e in feed:
        if e.film_id == str(film_id) and e.watched_date and abs((e.watched_date - watched).days) <= DUPLICATE_WINDOW_DAYS:
            return True
    return False


def earlier_in_feed(feed: list[DiaryEntry], film_id: str | None, watched: date) -> bool:
    if not film_id:
        return False
    return any(e.film_id == str(film_id) and e.watched_date and
               e.watched_date < watched - timedelta(days=DUPLICATE_WINDOW_DAYS) for e in feed)


class AuthProblem(Exception):
    """Letterboxd needs you to sign in again; stop this round instead of burning retries."""


class SyncEngine:
    def __init__(self, db: DB, letterboxd: LetterboxdClient, data_dir: str = "/data"):
        self.db = db
        self.lb = letterboxd
        self.data_dir = data_dir
        self._wake = asyncio.Event()
        self._round_lock = asyncio.Lock()
        self._feed_cache: tuple[float, list[DiaryEntry]] | None = None

    # ---- wiring --------------------------------------------------------------
    def plex(self) -> PlexClient | None:
        token = self.db.get("plex_token")
        if not token:
            return None
        client_id = self.db.get("plex_client_id")
        return PlexClient(client_id=client_id, token=token, server_url=self.db.get("plex_server_url"),
                          server_token=self.db.get("plex_server_token"), data_dir=self.data_dir)

    def wake(self) -> None:
        self._wake.set()

    def _status(self, key: str, ok: bool, message: str = "") -> None:
        self.db.set(key, {"ok": ok, "message": message, "at": int(time.time())})

    # ---- main loop ----------------------------------------------------------------
    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # never let one bad round kill the service
                log.exception("Sync round failed")
                self._status("last_round", False, str(e))
            interval = int(self.db.settings()["poll_seconds"])
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(30, interval))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def poll_once(self) -> dict[str, Any]:
        async with self._round_lock:
            summary = {"new": 0, "processed": 0}
            plex = self.plex()
            if not plex or not self.db.get("plex_server_url"):
                self._status("last_round", False, "Connect Plex to start syncing.")
                return summary
            try:
                summary["new"] = await self._collect(plex)
                self._status("plex_status", True, "Connected")
                if not (self.lb.signed_in() and self.db.get("lb_member_id")):
                    self._status("last_round", False, "Connect Letterboxd to start syncing.")
                    return summary
                try:
                    summary["processed"] = await self._process_due(plex)
                    await self._late_reviews(plex)
                except AuthProblem as e:
                    self._status("lb_status", False, str(e))
                    self._status("last_round", False, str(e))
                    return summary
            except PlexError as e:
                self._status("plex_status", False, str(e))
                self._status("last_round", False, str(e))
                return summary
            finally:
                await plex.close()
            self._status("last_round", True,
                          f"{summary['new']} new watch(es), {summary['processed']} processed")
            return summary

    # ---- step 1: pick up new watches from Plex -----------------------------------
    async def _collect(self, plex: PlexClient) -> int:
        now = int(time.time())
        sync_from = self.db.get("sync_from")
        if sync_from is None:
            # First run: start from now so your whole history doesn't flood your diary.
            self.db.set("sync_from", now)
            return 0
        settings = self.db.settings()
        last_seen = int(self.db.get("last_viewed_seen", sync_from))
        since = max(int(sync_from), last_seen - HISTORY_OVERLAP)
        items = await plex.movie_history(since, account_id=int(settings["plex_account_id"]))
        grace = int(settings["grace_minutes"]) * 60
        added = 0
        newest = last_seen
        for it in items:
            viewed = int(it.get("viewedAt") or 0)
            rk = str(it.get("ratingKey") or "")
            if not viewed or not rk or viewed < int(sync_from):
                continue
            newest = max(newest, viewed)
            if self.db.add_watch(history_key=f"{rk}:{viewed}", rating_key=rk, title=it.get("title"),
                                 year=it.get("year"), viewed_at=viewed, due_at=viewed + grace):
                added += 1
                log.info("New watch: %s (%s)", it.get("title"), it.get("year"))
        self.db.set("last_viewed_seen", newest)
        return added

    # ---- step 2: enrich and log due watches ----------------------------------------
    async def _process_due(self, plex: PlexClient) -> int:
        n = 0
        for w in self.db.due_watches(int(time.time())):
            await self.process_watch(plex, w)
            n += 1
        return n

    async def _feed(self) -> list[DiaryEntry]:
        if self._feed_cache and time.time() - self._feed_cache[0] < 120:
            return self._feed_cache[1]
        try:
            feed = await self.lb.diary(self.db.get("lb_member_id"))
        except NotSignedIn:
            raise
        except LetterboxdError as e:
            log.warning("Couldn't read your Letterboxd diary (duplicate check skipped): %s", e)
            feed = []
        self._feed_cache = (time.time(), feed)
        return feed

    async def _my_review(self, plex: PlexClient, plex_id: str | None):
        if not plex_id:
            return None
        try:
            review = await plex.my_review(plex_id, self.db.get("plex_user_id"), self.db.get("plex_username"))
            self._status("review_api_status", True, "Working")
            return review
        except PlexError as e:
            # Reviews are best-effort: a broken reviews API must not block watches and ratings.
            self._status("review_api_status", False, str(e))
            log.warning("Plex review lookup failed: %s", e)
            return None
        except Exception as e:  # network trouble etc.
            self._status("review_api_status", False, f"{type(e).__name__}: {e}")
            return None

    async def process_watch(self, plex: PlexClient, w: dict, force_post: bool = False) -> dict:
        settings = self.db.settings()
        try:
            meta = await plex.item(w["rating_key"])
            if meta is None:
                self.db.update_watch(w["id"], status="skipped",
                                     error="This movie was removed from your Plex library before it could be matched.")
                return self.db.watch(w["id"])
            guids = parse_guids(meta)
            rating = plex_rating_to_letterboxd(meta.get("userRating")) if settings["sync_ratings"] else None
            review_text, spoilers = None, False
            review = await self._my_review(plex, guids["plex"]) if settings["sync_reviews"] or settings["sync_ratings"] else None
            if review:
                if settings["sync_reviews"] and review.message:
                    review_text, spoilers = review.message, review.has_spoilers
                if settings["sync_ratings"] and rating is None and review.rating is not None:
                    rating = plex_rating_to_letterboxd(review.rating)

            film_row = self.db.cached_film(guids["tmdb"], guids["imdb"])
            if film_row:
                film = Film(film_id=film_row["film_id"], slug=film_row["slug"],
                            url=f"https://letterboxd.com/film/{film_row['slug']}/" if film_row["slug"]
                            else f"https://boxd.it/{film_row['film_id']}")
            else:
                film = await self.lb.resolve_film(guids["tmdb"], guids["imdb"])
                if film:
                    self.db.cache_film(guids["tmdb"], guids["imdb"], film.film_id, film.slug)
            if not film:
                self.db.update_watch(w["id"], status="skipped", tmdb_id=guids["tmdb"], imdb_id=guids["imdb"],
                                     error="Couldn't find this movie on Letterboxd (no TMDB/IMDb match).")
                return self.db.watch(w["id"])

            watched = watch_date(w["viewed_at"])
            feed = await self._feed()
            if is_duplicate(feed, film.film_id, watched):
                self.db.update_watch(w["id"], status="skipped", tmdb_id=guids["tmdb"], imdb_id=guids["imdb"],
                                     lb_film_id=film.film_id, lb_slug=film.slug,
                                     error="Already in your Letterboxd diary for this date.")
                return self.db.watch(w["id"])

            rewatch = False
            if settings["sync_rewatches"]:
                rewatch = await self._is_rewatch(plex, w, guids, film, feed, watched)

            self.db.update_watch(w["id"], title=meta.get("title") or w["title"], year=meta.get("year") or w["year"],
                                 tmdb_id=guids["tmdb"], imdb_id=guids["imdb"], plex_guid=guids["plex"],
                                 rating=rating, review=review_text, spoilers=int(spoilers), rewatch=int(rewatch),
                                 lb_film_id=film.film_id, lb_slug=film.slug, last_checked=int(time.time()))

            if settings["preview_mode"] and not force_post:
                self.db.update_watch(w["id"], status="preview", error=None)
                return self.db.watch(w["id"])

            viewing_id = await self.lb.create_entry(member_id=self.db.get("lb_member_id"), film=film,
                                                    watched=watched, rating10=rating, review=review_text,
                                                    spoilers=spoilers, rewatch=rewatch)
            self.db.update_watch(w["id"], status="synced", synced_at=int(time.time()), lb_viewing_id=viewing_id,
                                 review_synced=int(bool(review_text)), rating_synced=rating, error=None)
            self._status("lb_status", True, "Connected")
            self._feed_cache = None
            log.info("Logged %s to Letterboxd (%s)", meta.get("title"), watched)
            return self.db.watch(w["id"])

        except (NotSignedIn, NotConfigured) as e:
            self.db.update_watch(w["id"], error=str(e))
            raise AuthProblem(str(e)) from e
        except PlexError:
            raise
        except Exception as e:
            attempts = int(w.get("attempts") or 0) + 1
            fields: dict[str, Any] = {"attempts": attempts, "error": f"{type(e).__name__}: {e}"}
            if attempts >= MAX_ATTEMPTS:
                fields["status"] = "failed"
            else:
                fields["status"] = "waiting"
                fields["due_at"] = int(time.time()) + attempts * 15 * 60
            self.db.update_watch(w["id"], **fields)
            log.warning("Watch %s failed (attempt %s): %s", w.get("title"), attempts, e)
            return self.db.watch(w["id"])

    async def _is_rewatch(self, plex: PlexClient, w: dict, guids: dict, film: Film,
                          feed: list[DiaryEntry], watched: date) -> bool:
        if self.db.earlier_synced(guids["tmdb"], guids["imdb"], w["viewed_at"], w["id"]):
            return True
        if earlier_in_feed(feed, film.film_id, watched):
            return True
        try:
            history = await plex.item_history(w["rating_key"], int(self.db.settings()["plex_account_id"]))
        except Exception:
            history = []
        if any(int(h.get("viewedAt") or 0) < w["viewed_at"] - 60 for h in history):
            return True
        return False

    # ---- step 3: a review or rating written after the entry was logged ----------
    async def _late_reviews(self, plex: PlexClient) -> None:
        settings = self.db.settings()
        if not (settings["sync_reviews"] or settings["sync_ratings"]):
            return
        now = int(time.time())
        since = now - int(settings["late_review_days"]) * 86400
        for w in self.db.late_review_candidates(since, now - LATE_CHECK_EVERY):
            self.db.update_watch(w["id"], last_checked=now)
            meta = await plex.item(w["rating_key"])
            rating = w["rating"]
            if settings["sync_ratings"] and meta:
                rating = plex_rating_to_letterboxd(meta.get("userRating")) or rating
            review_text, spoilers = w["review"], bool(w["spoilers"])
            if settings["sync_reviews"]:
                review = await self._my_review(plex, w["plex_guid"])
                if review and review.message:
                    review_text, spoilers = review.message, review.has_spoilers
                if review and settings["sync_ratings"] and rating is None and review.rating is not None:
                    rating = plex_rating_to_letterboxd(review.rating)
            changed_review = bool(review_text) and review_text != (w["review"] if w["review_synced"] else None)
            changed_rating = rating is not None and rating != w["rating_synced"]
            if not (changed_review or changed_rating):
                continue
            if not w["lb_viewing_id"]:
                self.db.update_watch(w["id"], review=review_text, rating=rating, spoilers=int(spoilers),
                                     error="You added a review/rating on Plex after this was logged, but "
                                           "the Letterboxd entry couldn't be found to update. Add it on Letterboxd by hand.")
                continue
            try:
                await self.lb.update_entry(w["lb_viewing_id"], rating10=rating, review=review_text,
                                           spoilers=spoilers)
                self.db.update_watch(w["id"], review=review_text, rating=rating, spoilers=int(spoilers),
                                     review_synced=int(bool(review_text)), rating_synced=rating, error=None)
                log.info("Updated %s on Letterboxd with your later review/rating", w["title"])
            except (NotSignedIn, NotConfigured) as e:
                raise AuthProblem(str(e)) from e
            except LetterboxdError as e:
                self.db.update_watch(w["id"], error=f"Couldn't update with your later review: {e}")

    # ---- dashboard actions -----------------------------------------------------------
    async def approve(self, watch_id: int) -> dict | None:
        """Post a previewed (or failed) entry now, re-reading its rating/review first."""
        w = self.db.watch(watch_id)
        if not w:
            return None
        plex = self.plex()
        if not plex:
            return w
        async with self._round_lock:
            try:
                self.db.update_watch(watch_id, status="waiting", attempts=0, error=None)
                return await self.process_watch(plex, self.db.watch(watch_id), force_post=True)
            except AuthProblem as e:
                self._status("lb_status", False, str(e))
                return self.db.watch(watch_id)
            finally:
                await plex.close()

    def retry(self, watch_id: int) -> None:
        self.db.update_watch(watch_id, status="waiting", attempts=0, error=None, due_at=int(time.time()))
        self.wake()

    def skip(self, watch_id: int) -> None:
        self.db.update_watch(watch_id, status="skipped", error="Skipped by you.")
