"""FastAPI app: the dashboard, sign-in flows, and the background sync loop."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import ui
from .db import DB
from .letterboxd import LetterboxdClient, LetterboxdError
from .plex import PlexClient
from .sync import SyncEngine

log = logging.getLogger("plexboxd.web")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")
LB_CLIENT_ID = os.environ.get("LETTERBOXD_CLIENT_ID", "").strip()
LB_CLIENT_SECRET = os.environ.get("LETTERBOXD_CLIENT_SECRET", "").strip()
# The address you open the dashboard at, e.g. http://192.168.1.50:8977. Used to build the
# Letterboxd sign-in redirect URL; if unset, it's taken from the address in your browser.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")


class State:
    db: DB
    lb: LetterboxdClient
    engine: SyncEngine
    task: asyncio.Task | None = None


S = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.db = DB(os.path.join(DATA_DIR, "plexboxd.db"))
    if not S.db.get("plex_client_id"):
        S.db.set("plex_client_id", str(uuid.uuid4()))
    S.lb = LetterboxdClient(LB_CLIENT_ID, LB_CLIENT_SECRET,
                            load_tokens=lambda: S.db.get("lb_tokens"),
                            save_tokens=_save_lb_tokens)
    S.engine = SyncEngine(S.db, S.lb, DATA_DIR)
    if os.environ.get("PLEXBOXD_NO_LOOP") != "1":
        S.task = asyncio.create_task(S.engine.run_forever())
    yield
    if S.task:
        S.task.cancel()
    await S.lb.close()


def _save_lb_tokens(tokens: dict | None) -> None:
    if tokens:
        S.db.set("lb_tokens", tokens)
    else:
        S.db.delete("lb_tokens")


app = FastAPI(title="Plexboxd", lifespan=lifespan)

PUBLIC_PATHS = {"/plex/callback", "/letterboxd/callback", "/health"}


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    # Optional password for the dashboard (set UI_PASSWORD). The Plex sign-in callback stays
    # reachable so plex.tv can send you back here.
    if UI_PASSWORD and request.url.path not in PUBLIC_PATHS:
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, UI_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Sign in required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Plexboxd"'})
    return await call_next(request)


def go(path: str = "/", msg: str | None = None, kind: str = "") -> RedirectResponse:
    if msg:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}msg={quote(msg)}&kind={quote(kind)}"
    return RedirectResponse(path, status_code=303)


def context(request: Request) -> dict:
    db = S.db
    msg = request.query_params.get("msg")
    return {
        "settings": db.settings(),
        "flash": (request.query_params.get("kind", ""), msg) if msg else None,
        "plex_connected": bool(db.get("plex_token")),
        "plex_username": db.get("plex_username"),
        "plex_server_url": db.get("plex_server_url"),
        "plex_server_name": db.get("plex_server_name"),
        "plex_status": db.get("plex_status"),
        "lb_username": db.get("lb_username") if S.lb.signed_in() else None,
        "lb_configured": S.lb.configured,
        "lb_redirect_hint": (PUBLIC_URL or "http://<your-server-ip>:8977") + "/letterboxd/callback",
        "lb_status": db.get("lb_status"),
        "review_api_status": db.get("review_api_status"),
        "last_round": db.get("last_round"),
        "sync_from": db.get("sync_from"),
        "counts": db.counts(),
        "watches": db.recent_watches(100),
    }


def plex_client() -> PlexClient:
    return PlexClient(client_id=S.db.get("plex_client_id"), token=S.db.get("plex_token"),
                      server_url=S.db.get("plex_server_url"), server_token=S.db.get("plex_server_token"),
                      data_dir=DATA_DIR)


# ---- dashboard -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return ui.dashboard(context(request))


@app.get("/health")
async def health():
    return {"ok": True, "last_round": S.db.get("last_round")}


@app.post("/sync-now")
async def sync_now():
    S.engine.wake()
    await asyncio.sleep(0.5)
    return go("/", "Checking Plex now. Refresh in a few seconds.")


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    changes = {
        "preview_mode": form.get("preview_mode") == "1",
        "sync_ratings": form.get("sync_ratings") == "1",
        "sync_reviews": form.get("sync_reviews") == "1",
        "sync_rewatches": form.get("sync_rewatches") == "1",
    }
    for key, lo in (("grace_minutes", 0), ("poll_seconds", 30), ("late_review_days", 0), ("plex_account_id", 1)):
        try:
            changes[key] = max(lo, int(form.get(key, "")))
        except ValueError:
            pass
    S.db.update_settings(**changes)
    sf = form.get("sync_from")
    if sf:
        try:
            new_from = int(datetime.strptime(str(sf), "%Y-%m-%d").timestamp())
            if new_from != S.db.get("sync_from"):
                S.db.set("sync_from", new_from)
                S.db.set("last_viewed_seen", new_from)
        except ValueError:
            pass
    S.engine.wake()
    return go("/", "Settings saved.")


@app.post("/watch/{watch_id}/post")
async def post_watch(watch_id: int):
    if not S.lb.signed_in():
        return go("/", "Sign in with Letterboxd first.", "bad")
    w = await S.engine.approve(watch_id)
    if w and w["status"] == "synced":
        return go("/", f"Logged {w['title']} to Letterboxd.")
    return go("/", f"Couldn't post: {(w or {}).get('error') or 'unknown error'}", "bad")


@app.post("/watch/{watch_id}/retry")
async def retry_watch(watch_id: int):
    S.engine.retry(watch_id)
    return go("/", "Queued for another try.")


@app.post("/watch/{watch_id}/skip")
async def skip_watch(watch_id: int):
    S.engine.skip(watch_id)
    return go("/")


# ---- Plex sign-in ------------------------------------------------------------------

@app.post("/plex/start")
async def plex_start(request: Request):
    plex = plex_client()
    try:
        pin = await plex.create_pin()
    except Exception as e:
        return go("/", f"Couldn't reach plex.tv: {e}", "bad")
    finally:
        await plex.close()
    S.db.set("plex_pin", pin["id"])
    forward = str(request.base_url).rstrip("/") + "/plex/callback"
    return RedirectResponse(plex.auth_url(pin["code"], forward), status_code=303)


@app.get("/plex/callback")
async def plex_callback():
    pin_id = S.db.get("plex_pin")
    if not pin_id:
        return go("/", "Plex sign-in wasn't started from here. Try again.", "bad")
    plex = plex_client()
    try:
        token = None
        for _ in range(5):  # plex.tv can take a moment to attach the token to the PIN
            token = await plex.check_pin(pin_id)
            if token:
                break
            await asyncio.sleep(1)
        if not token:
            return go("/", "Plex sign-in didn't finish. Try again.", "bad")
        S.db.set("plex_token", token)
        S.db.delete("plex_pin")
        plex.token = token
        acct = await plex.account()
        S.db.set("plex_username", acct.get("username") or acct.get("title"))
        S.db.set("plex_user_id", acct.get("uuid") or str(acct.get("id")))
        S.db.set("plex_status", {"ok": True, "message": "Connected", "at": int(time.time())})
    except Exception as e:
        return go("/", f"Plex sign-in failed: {e}", "bad")
    finally:
        await plex.close()
    if S.db.get("plex_server_url"):
        return go("/", "Signed in to Plex.")
    return go("/setup/server")


@app.get("/setup/server", response_class=HTMLResponse)
async def server_page(request: Request):
    if not S.db.get("plex_token"):
        return go("/", "Sign in to Plex first.", "bad")
    plex = plex_client()
    try:
        servers = await plex.servers()
        S.db.set("plex_servers_cache", servers)
        return ui.server_picker(servers, S.db.get("plex_server_url"), request.query_params.get("msg"))
    except Exception as e:
        return ui.server_picker([], S.db.get("plex_server_url"), f"Couldn't list your servers: {e}")
    finally:
        await plex.close()


@app.post("/setup/server")
async def server_save(choice: str = Form(""), manual: str = Form("")):
    servers = S.db.get("plex_servers_cache", []) or []
    name, url, token = None, None, S.db.get("plex_token")
    if manual.strip():
        url = manual.strip().rstrip("/")
        if not url.startswith("http"):
            url = "http://" + url
        name = url
    elif "|" in choice:
        name, url = choice.split("|", 1)
        for s in servers:
            if s["name"] == name:
                token = s.get("token") or token
    if not url:
        return go("/setup/server", "Pick a server or enter its address.")
    plex = PlexClient(client_id=S.db.get("plex_client_id"), token=S.db.get("plex_token"),
                      server_url=url, server_token=token, data_dir=DATA_DIR)
    try:
        await plex.test_server()
    except Exception as e:
        return go("/setup/server", f"Couldn't connect to {url}: {e}")
    finally:
        await plex.close()
    S.db.set("plex_server_url", url)
    S.db.set("plex_server_token", token)
    S.db.set("plex_server_name", name)
    S.engine.wake()
    return go("/", f"Following watch history on {name}. Watches from now on will sync.")


# ---- Letterboxd sign-in (official API, OAuth 2.0) ----------------------------------------

def lb_redirect_uri(request: Request) -> str:
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/letterboxd/callback"


@app.post("/letterboxd/start")
async def lb_start(request: Request):
    if not S.lb.configured:
        return go("/", "Add your own LETTERBOXD_CLIENT_ID and LETTERBOXD_CLIENT_SECRET to your .env file "
                       "and restart the container first.", "bad")
    state = secrets.token_urlsafe(24)
    S.db.set("lb_oauth_state", state)
    return RedirectResponse(S.lb.authorize_url(lb_redirect_uri(request), state), status_code=303)


@app.get("/letterboxd/callback")
async def lb_callback(request: Request):
    params = request.query_params
    if params.get("error"):
        return go("/", f"Letterboxd sign-in was cancelled ({params.get('error_description') or params['error']}).", "bad")
    expected = S.db.get("lb_oauth_state")
    if not expected or not secrets.compare_digest(params.get("state", ""), expected):
        return go("/", "Letterboxd sign-in didn't match a sign-in started here. Please try again.", "bad")
    code = params.get("code")
    if not code:
        return go("/", "Letterboxd didn't send a sign-in code back. Please try again.", "bad")
    S.db.delete("lb_oauth_state")
    try:
        await S.lb.exchange_code(code, lb_redirect_uri(request))
        me = await S.lb.me()
    except LetterboxdError as e:
        return go("/", str(e), "bad")
    except Exception as e:
        return go("/", f"Letterboxd sign-in failed: {type(e).__name__}: {e}", "bad")
    S.db.set("lb_member_id", me["id"])
    S.db.set("lb_username", me["username"])
    S.db.set("lb_status", {"ok": True, "message": "Connected", "at": int(time.time())})
    S.engine.wake()
    return go("/", f"Signed in to Letterboxd as @{me['username']}.")


@app.post("/letterboxd/logout")
async def lb_logout():
    S.lb.sign_out()
    for key in ("lb_username", "lb_member_id", "lb_status"):
        S.db.delete(key)
    return go("/", "Signed out of Letterboxd. Watches will wait until you sign in again.")


# ---- diagnostics -------------------------------------------------------------------

async def run_doctor() -> list[tuple[str, bool | None, str]]:
    report: list[tuple[str, bool | None, str]] = []
    plex = plex_client()
    recent_plex_id = None
    try:
        if not S.db.get("plex_token"):
            report.append(("Plex sign-in", False, "Not signed in."))
        else:
            try:
                acct = await plex.account()
                report.append(("Plex sign-in", True, f"Signed in as {acct.get('username')} (uuid {acct.get('uuid')})"))
            except Exception as e:
                report.append(("Plex sign-in", False, str(e)))
            try:
                ident = await plex.test_server()
                report.append(("Plex server", True, f"{S.db.get('plex_server_url')} - version {ident.get('version')}"))
                hist = await plex.movie_history(0, int(S.db.settings()["plex_account_id"]), page_size=20, max_pages=1)
                if hist:
                    h = hist[0]
                    meta = await plex.item(str(h.get("ratingKey")))
                    from .plex import parse_guids
                    guids = parse_guids(meta or {})
                    recent_plex_id = guids["plex"]
                    report.append(("Latest movie in your Plex history", True,
                                   f"{h.get('title')} ({h.get('year')}) - guids {guids} - your rating {(meta or {}).get('userRating')}"))
                else:
                    report.append(("Latest movie in your Plex history", None,
                                   "No movie plays found for this account ID. Check the Plex account ID setting."))
            except Exception as e:
                report.append(("Plex server", False, str(e)))
            try:
                fields = await plex.introspect_review_fields()
                report.append(("Plex reviews API fields", None, "\n".join(fields) or "No review/activity fields listed."))
            except Exception as e:
                report.append(("Plex reviews API fields", None, f"Introspection not available: {e}"))
            if recent_plex_id:
                try:
                    rv = await plex.my_review(recent_plex_id, S.db.get("plex_user_id"), S.db.get("plex_username"))
                    report.append(("Plex review lookup (latest movie)", True,
                                   f"Found your review: {rv.message[:200]!r} (rating {rv.rating}, spoilers {rv.has_spoilers})"
                                   if rv else "Query works; you haven't reviewed that movie."))
                except Exception as e:
                    report.append(("Plex review lookup (latest movie)", False, str(e)))
    finally:
        await plex.close()

    if not S.lb.configured:
        report.append(("Letterboxd API credentials", False,
                       "LETTERBOXD_CLIENT_ID / LETTERBOXD_CLIENT_SECRET aren't set. Request your own API access from "
                       "Letterboxd (api@letterboxd.com) and add them to your .env file."))
        return report
    report.append(("Letterboxd API credentials", True, f"Client ID {S.lb.client_id[:6]}... is set"))
    if not S.lb.signed_in():
        report.append(("Letterboxd sign-in", False, "Not signed in. Use 'Sign in with Letterboxd' on the dashboard."))
        return report
    try:
        me = await S.lb.me()
        report.append(("Letterboxd sign-in", True, f"Signed in as @{me['username']} (member {me['id']})"))
    except Exception as e:
        report.append(("Letterboxd sign-in", False, f"{type(e).__name__}: {e}"))
        return report
    try:
        film = await S.lb.resolve_film("550", "tt0137523")
        report.append(("Letterboxd film lookup (TMDB 550)", bool(film),
                       f"{film.name} ({film.year}) - film id {film.film_id}" if film else "No match returned."))
    except Exception as e:
        report.append(("Letterboxd film lookup (TMDB 550)", False, f"{type(e).__name__}: {e}"))
    member_id = S.db.get("lb_member_id")
    if member_id:
        try:
            feed = await S.lb.diary(member_id, per_page=20)
            report.append(("Letterboxd diary", True, f"{len(feed)} recent diary entries readable"))
        except Exception as e:
            report.append(("Letterboxd diary", False, f"{type(e).__name__}: {e}"))
    return report


@app.get("/doctor", response_class=HTMLResponse)
async def doctor():
    return ui.doctor_page(await run_doctor())


@app.get("/doctor.json")
async def doctor_json():
    return JSONResponse([{"check": n, "ok": ok, "detail": d} for n, ok, d in await run_doctor()])
