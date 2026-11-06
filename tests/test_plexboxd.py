import asyncio
import json
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from plexboxd import ui
from plexboxd.db import DB
from plexboxd.letterboxd import (DiaryEntry, Film, LetterboxdClient, NotConfigured, NotSignedIn,
                                 build_creation_request, build_update_request, entry_from_api, film_from_api,
                                 to_letterboxd_rating)
from plexboxd.plex import PlexError, PlexReview, _find_review_nodes, parse_guids, plex_rating_to_letterboxd
from plexboxd.sync import SyncEngine, is_duplicate, watch_date


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- Plex helpers

def test_parse_guids_modern_agent():
    meta = {"guid": "plex://movie/5d7768ba96b655001fdc0408",
            "Guid": [{"id": "imdb://tt0137523"}, {"id": "tmdb://550"}, {"id": "tvdb://123"}]}
    assert parse_guids(meta) == {"tmdb": "550", "imdb": "tt0137523", "plex": "5d7768ba96b655001fdc0408"}


def test_parse_guids_legacy_agent():
    assert parse_guids({"guid": "com.plexapp.agents.imdb://tt0111161?lang=en"})["imdb"] == "tt0111161"


@pytest.mark.parametrize("plex,lb", [(None, None), (0, None), (1, 1), (7, 7), (8.6, 9), (10, 10), (11, 10), ("x", None)])
def test_plex_rating_mapping(plex, lb):
    assert plex_rating_to_letterboxd(plex) == lb


def test_review_node_walk():
    data = {"activityFeed": {"nodes": [
        {"__typename": "ActivityWatchHistory", "userV2": {"id": "u1"}},
        {"__typename": "ActivityReview", "message": "Loved it", "userV2": {"id": "u1", "username": "me"}},
    ]}}
    nodes = _find_review_nodes(data)
    assert len(nodes) == 1 and nodes[0]["message"] == "Loved it"


# ---------------------------------------------------------------- Letterboxd API helpers

@pytest.mark.parametrize("r10,r5", [(None, None), (0, None), (1, 0.5), (7, 3.5), (9, 4.5), (10, 5.0)])
def test_letterboxd_rating(r10, r5):
    assert to_letterboxd_rating(r10) == r5


def test_creation_request():
    body = build_creation_request(film_id="2b8a", watched=date(2026, 11, 5), rating10=9,
                                  review="Great.", spoilers=True, rewatch=False)
    assert body == {"filmId": "2b8a", "diaryDetails": {"diaryDate": "2026-11-05", "rewatch": False},
                    "rating": 4.5, "review": {"text": "Great.", "containsSpoilers": True}}
    bare = build_creation_request(film_id="2b8a", watched=date(2026, 11, 5), rating10=None,
                                  review=None, spoilers=False, rewatch=True)
    assert "rating" not in bare and "review" not in bare and bare["diaryDetails"]["rewatch"] is True
    assert build_update_request(rating10=None, review=None, spoilers=False) == {}


def test_film_and_entry_parsing():
    f = film_from_api({"id": "2b8a", "name": "Fight Club", "releaseYear": 1999,
                       "links": [{"type": "tmdb", "url": "https://themoviedb.org/movie/550"},
                                 {"type": "letterboxd", "url": "https://letterboxd.com/film/fight-club/"}]})
    assert (f.film_id, f.slug, f.year) == ("2b8a", "fight-club", 1999)
    e = entry_from_api({"id": "L1", "film": {"id": "2b8a"}, "diaryDetails": {"diaryDate": "2026-11-01", "rewatch": True}})
    assert (e.entry_id, e.film_id, e.watched_date, e.rewatch) == ("L1", "2b8a", date(2026, 11, 1), True)


def test_duplicate_window():
    feed = [DiaryEntry("L1", "2b8a", date(2026, 11, 4), False)]
    assert is_duplicate(feed, "2b8a", date(2026, 11, 5))
    assert not is_duplicate(feed, "2b8a", date(2026, 11, 7))
    assert not is_duplicate(feed, "zzzz", date(2026, 11, 4))


# ---------------------------------------------------------------- Letterboxd API client (mocked HTTP)

class FakeLetterboxdServer:
    def __init__(self):
        self.calls = []
        self.token_counter = 0
        self.entries = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path.replace("/api/v0", "")
        if path == "/auth/token":
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["cid"] and form["client_secret"] == ["secret"]
            if form["grant_type"] == ["refresh_token"] and form["refresh_token"] != ["r1"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.token_counter += 1
            return httpx.Response(200, json={"access_token": f"a{self.token_counter}", "refresh_token": "r1",
                                             "expires_in": 3600})
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer a"):
            return httpx.Response(401)
        if path == "/me":
            return httpx.Response(200, json={"member": {"id": "M1", "username": "RamenNuudls"}})
        if path == "/films":
            ids = request.url.params.get_list("filmId")
            if "tmdb:550" in ids:
                return httpx.Response(200, json={"items": [{"id": "2b8a", "name": "Fight Club", "releaseYear": 1999,
                                                            "links": [{"type": "letterboxd",
                                                                       "url": "https://letterboxd.com/film/fight-club/"}]}]})
            return httpx.Response(200, json={"items": []})
        if path == "/log-entries" and request.method == "POST":
            body = json.loads(request.content)
            self.entries.insert(0, {"id": f"LE{len(self.entries) + 1}", "film": {"id": body["filmId"]},
                                    "diaryDetails": body["diaryDetails"], "body": body})
            return httpx.Response(200, json={})
        if path == "/log-entries" and request.method == "GET":
            film = request.url.params.get("film")
            items = [e for e in self.entries if not film or e["film"]["id"] == film]
            return httpx.Response(200, json={"items": items})
        if path.startswith("/log-entry/") and request.method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(404)


def make_client(server, tokens=None):
    store = {"t": tokens}
    client = LetterboxdClient("cid", "secret", load_tokens=lambda: store["t"],
                              save_tokens=lambda t: store.__setitem__("t", t),
                              transport=httpx.MockTransport(server))
    return client, store


def test_oauth_flow_and_logging():
    server = FakeLetterboxdServer()
    client, store = make_client(server)
    url = urlparse(client.authorize_url("http://nas:8977/letterboxd/callback", "st8"))
    q = parse_qs(url.query)
    assert url.path.endswith("/auth/authorize") and q["response_type"] == ["code"] and q["state"] == ["st8"]
    assert "content:modify" in q["scope"][0] and q["redirect_uri"] == ["http://nas:8977/letterboxd/callback"]

    async def flow():
        await client.exchange_code("code123", "http://nas:8977/letterboxd/callback")
        me = await client.me()
        film = await client.resolve_film("550", "tt0137523")
        entry_id = await client.create_entry(member_id=me["id"], film=film, watched=date(2026, 11, 5),
                                             rating10=9, review="Great", spoilers=False, rewatch=False)
        await client.update_entry(entry_id, rating10=10, review="Even better", spoilers=True)
        diary = await client.diary(me["id"])
        missing = await client.resolve_film("999999", None)
        await client.close()
        return me, film, entry_id, diary, missing

    me, film, entry_id, diary, missing = run(flow())
    assert store["t"]["access_token"] == "a1" and me["username"] == "RamenNuudls"
    assert film.film_id == "2b8a" and entry_id == "LE1" and missing is None
    assert diary[0].film_id == "2b8a" and diary[0].watched_date == date(2026, 11, 5)
    patch = [c for c in server.calls if c.method == "PATCH"][0]
    assert patch.url.path.endswith("/log-entry/LE1")
    assert json.loads(patch.content) == {"rating": 5.0, "review": {"text": "Even better", "containsSpoilers": True}}
    assert all("letterboxd.com/" not in str(c.url) or "api.letterboxd.com" in str(c.url) for c in server.calls)


def test_token_refresh_and_expiry():
    server = FakeLetterboxdServer()
    client, store = make_client(server, {"access_token": "a0", "refresh_token": "r1", "expires_at": 0})
    run(client.me())
    assert store["t"]["access_token"] == "a1"  # refreshed because it had expired

    client2, store2 = make_client(server, {"access_token": "a0", "refresh_token": "bad", "expires_at": 0})
    with pytest.raises(NotSignedIn):
        run(client2.me())
    assert store2["t"] is None  # dead sign-in is cleared


def test_missing_credentials():
    client = LetterboxdClient("", "", load_tokens=lambda: None, save_tokens=lambda t: None)
    assert not client.configured
    with pytest.raises(NotConfigured):
        client.authorize_url("http://x/letterboxd/callback", "s")


# ---------------------------------------------------------------- sync engine with fakes

class FakePlex:
    def __init__(self):
        self.history, self.items, self.item_hist, self.reviews = [], {}, {}, {}
        self.review_error = None

    async def close(self):
        pass

    async def movie_history(self, since, account_id=None, page_size=200, max_pages=25):
        return [h for h in self.history if h["viewedAt"] >= since]

    async def item(self, rk):
        return self.items.get(rk)

    async def item_history(self, rk, account_id=None):
        return self.item_hist.get(rk, [])

    async def my_review(self, plex_id, uid, uname):
        if self.review_error:
            raise PlexError(self.review_error)
        return self.reviews.get(plex_id)


class FakeLB:
    def __init__(self):
        self.created, self.updated, self.feed = [], [], []
        self.fail_with = None

    def signed_in(self):
        return True

    async def resolve_film(self, tmdb, imdb):
        return Film(film_id="2b8a", slug="fight-club", url="https://letterboxd.com/film/fight-club/") if tmdb == "550" else None

    async def diary(self, member_id, per_page=100):
        return self.feed

    async def create_entry(self, **kw):
        if self.fail_with:
            raise self.fail_with
        self.created.append(kw)
        return "LE9"

    async def update_entry(self, entry_id, **kw):
        self.updated.append((entry_id, kw))


class FakeEngine(SyncEngine):
    def __init__(self, db, lb, plex):
        super().__init__(db, lb, "/tmp")
        self._plex = plex

    def plex(self):
        return self._plex


@pytest.fixture
def setup(tmp_path):
    db = DB(tmp_path / "t.db")
    db.set("plex_token", "tok")
    db.set("plex_server_url", "http://pms:32400")
    db.set("lb_member_id", "M1")
    db.update_settings(grace_minutes=0)
    plex, lb = FakePlex(), FakeLB()
    plex.items["100"] = {"title": "Fight Club", "year": 1999, "userRating": 9.0, "viewCount": 1,
                         "guid": "plex://movie/abc", "Guid": [{"id": "tmdb://550"}, {"id": "imdb://tt0137523"}]}
    return db, plex, lb, FakeEngine(db, lb, plex)


def add_play(plex, ts, rk="100"):
    plex.history.append({"type": "movie", "ratingKey": rk, "title": "Fight Club", "year": 1999, "viewedAt": ts})


def start(db):
    db.set("sync_from", int(time.time()) - 7200)


def test_first_run_is_baseline(setup):
    db, plex, lb, eng = setup
    add_play(plex, int(time.time()) - 86400)
    assert run(eng.poll_once())["new"] == 0 and db.get("sync_from") is not None and db.recent_watches() == []


def test_preview_then_post_with_review(setup):
    db, plex, lb, eng = setup
    start(db)
    ts = int(time.time()) - 3600
    add_play(plex, ts)
    plex.reviews["abc"] = PlexReview("Tyler would approve.", True, 9.0, "2026-11-05")
    run(eng.poll_once())
    w = db.recent_watches()[0]
    assert w["status"] == "preview" and w["rating"] == 9 and w["review"] == "Tyler would approve." and lb.created == []
    w = run(eng.approve(w["id"]))
    assert w["status"] == "synced" and w["lb_viewing_id"] == "LE9"
    c = lb.created[0]
    assert c["member_id"] == "M1" and c["rating10"] == 9 and c["review"] == "Tyler would approve."
    assert c["spoilers"] is True and c["rewatch"] is False and c["watched"] == watch_date(ts)


def test_auto_post_and_rewatch(setup):
    db, plex, lb, eng = setup
    db.update_settings(preview_mode=False)
    start(db)
    ts = int(time.time()) - 3600
    add_play(plex, ts)
    plex.item_hist["100"] = [{"viewedAt": ts}, {"viewedAt": ts - 400 * 86400}]
    run(eng.poll_once())
    assert db.recent_watches()[0]["status"] == "synced" and lb.created[0]["rewatch"] is True


def test_rewatch_from_diary(setup):
    db, plex, lb, eng = setup
    db.update_settings(preview_mode=False)
    start(db)
    ts = int(time.time()) - 3600
    add_play(plex, ts)
    lb.feed = [DiaryEntry("LE1", "2b8a", date(2020, 1, 1), False)]
    run(eng.poll_once())
    assert lb.created[0]["rewatch"] is True


def test_duplicate_skipped(setup):
    db, plex, lb, eng = setup
    db.update_settings(preview_mode=False)
    start(db)
    ts = int(time.time()) - 3600
    add_play(plex, ts)
    lb.feed = [DiaryEntry("LE1", "2b8a", watch_date(ts), False)]
    run(eng.poll_once())
    assert db.recent_watches()[0]["status"] == "skipped" and lb.created == []


def test_grace_window_holds_entry(setup):
    db, plex, lb, eng = setup
    db.update_settings(grace_minutes=60, preview_mode=False)
    start(db)
    add_play(plex, int(time.time()) - 60)
    run(eng.poll_once())
    assert db.recent_watches()[0]["status"] == "waiting" and lb.created == []


def test_review_api_failure_does_not_block(setup):
    db, plex, lb, eng = setup
    db.update_settings(preview_mode=False)
    start(db)
    add_play(plex, int(time.time()) - 3600)
    plex.review_error = "Cannot query field activityFeed"
    run(eng.poll_once())
    assert db.recent_watches()[0]["status"] == "synced" and lb.created[0]["rating10"] == 9
    assert db.get("review_api_status")["ok"] is False


def test_signed_out_letterboxd_holds_entries(setup):
    db, plex, lb, eng = setup
    db.update_settings(preview_mode=False)
    start(db)
    add_play(plex, int(time.time()) - 3600)
    lb.fail_with = NotSignedIn("expired")
    run(eng.poll_once())
    w = db.recent_watches()[0]
    assert w["status"] == "waiting" and w["attempts"] == 0 and db.get("lb_status")["ok"] is False


def test_errors_back_off_then_fail(setup):
    db, plex, lb, eng = setup
    db.update_settings(preview_mode=False)
    start(db)
    add_play(plex, int(time.time()) - 3600)
    lb.fail_with = RuntimeError("boom")
    run(eng.poll_once())
    w = db.recent_watches()[0]
    assert w["status"] == "waiting" and w["attempts"] == 1 and w["due_at"] > time.time()
    for _ in range(4):
        db.update_watch(w["id"], due_at=0)
        run(eng.poll_once())
    assert db.recent_watches()[0]["status"] == "failed"


def test_late_review_updates_entry(setup):
    db, plex, lb, eng = setup
    db.update_settings(preview_mode=False)
    start(db)
    add_play(plex, int(time.time()) - 3600)
    plex.items["100"]["userRating"] = None
    run(eng.poll_once())
    assert lb.created[0]["review"] is None and lb.created[0]["rating10"] is None
    plex.items["100"]["userRating"] = 8.0
    plex.reviews["abc"] = PlexReview("Better the second time.", False, 8.0, "2026-11-05")
    w = db.recent_watches()[0]
    db.update_watch(w["id"], last_checked=0)
    run(eng.poll_once())
    assert lb.updated == [("LE9", {"rating10": 8, "review": "Better the second time.", "spoilers": False})]
    db.update_watch(w["id"], last_checked=0)
    run(eng.poll_once())
    assert len(lb.updated) == 1  # nothing new, no repeat update


def test_no_letterboxd_match_skipped(setup):
    db, plex, lb, eng = setup
    start(db)
    plex.items["200"] = {"title": "Home video", "guid": "local://1", "Guid": []}
    add_play(plex, int(time.time()) - 3600, rk="200")
    run(eng.poll_once())
    assert db.recent_watches()[0]["status"] == "skipped"


# ---------------------------------------------------------------- UI + web app

def test_dashboard_renders(setup):
    db, plex, lb, eng = setup
    start(db)
    add_play(plex, int(time.time()) - 3600)
    plex.reviews["abc"] = PlexReview("<script>x</script>", False, None, None)
    run(eng.poll_once())
    html = ui.dashboard({"settings": db.settings(), "watches": db.recent_watches(), "counts": db.counts(),
                         "plex_connected": True, "plex_server_url": "x", "lb_username": "RamenNuudls"})
    assert "Fight Club" in html and "★★★★½" in html and "<script>x</script>" not in html


def test_web_app_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXBOXD_NO_LOOP", "1")
    import plexboxd.web as web
    monkeypatch.setattr(web, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(web, "LB_CLIENT_ID", "")
    monkeypatch.setattr(web, "LB_CLIENT_SECRET", "")
    from fastapi.testclient import TestClient
    with TestClient(web.app) as c:
        r = c.get("/")
        assert r.status_code == 200 and "Sign in with Plex" in r.text and "LETTERBOXD_CLIENT_ID" in r.text
        r = c.post("/letterboxd/start", follow_redirects=False)
        assert r.status_code == 303 and "LETTERBOXD_CLIENT_ID" in r.headers["location"]
        r = c.post("/settings", data={"grace_minutes": "15", "poll_seconds": "120", "late_review_days": "3",
                                      "plex_account_id": "1", "sync_ratings": "1"}, follow_redirects=False)
        assert r.status_code == 303
        s = web.S.db.settings()
        assert s["grace_minutes"] == 15 and s["preview_mode"] is False and s["sync_reviews"] is False
        assert c.get("/health").json()["ok"] is True


def test_web_app_letterboxd_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXBOXD_NO_LOOP", "1")
    import plexboxd.web as web
    monkeypatch.setattr(web, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(web, "LB_CLIENT_ID", "cid")
    monkeypatch.setattr(web, "LB_CLIENT_SECRET", "secret")
    monkeypatch.setattr(web, "PUBLIC_URL", "http://nas:8977")
    server = FakeLetterboxdServer()
    real_init = LetterboxdClient.__init__

    def patched_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(server)
        real_init(self, *a, **kw)
    monkeypatch.setattr(LetterboxdClient, "__init__", patched_init)
    from fastapi.testclient import TestClient
    with TestClient(web.app) as c:
        assert "Sign in with Letterboxd" in c.get("/").text
        r = c.post("/letterboxd/start", follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["redirect_uri"] == ["http://nas:8977/letterboxd/callback"]
        bad = c.get("/letterboxd/callback?code=x&state=wrong", follow_redirects=False)
        assert "didn" in bad.headers["location"]
        ok = c.get(f"/letterboxd/callback?code=x&state={q['state'][0]}", follow_redirects=False)
        assert "RamenNuudls" in ok.headers["location"]
        assert web.S.db.get("lb_member_id") == "M1" and web.S.lb.signed_in()
        c.post("/letterboxd/logout")
        assert not web.S.lb.signed_in()
