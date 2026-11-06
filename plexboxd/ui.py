"""Server-rendered HTML for the Plexboxd dashboard (no build step, no JS framework)."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

CSS = """
:root{
  --bg:#0f1216;--panel:#171b21;--panel2:#1d222a;--line:#2a313b;--text:#e7ebf0;--muted:#8b96a5;
  --accent:#e5a00d;--good:#3ecf8e;--warn:#f5b84a;--bad:#ff6b6b;--info:#6aa9ff;
  --radius:12px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--info)}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:20px;letter-spacing:.2px}
.logo{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),#ff7a3d);color:#111;font-weight:900}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);font-size:13px;color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--good)}.dot.bad{background:var(--bad)}.dot.warn{background:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:18px}
.card h2{margin:0 0 6px;font-size:16px}
.card p{margin:0 0 12px;color:var(--muted)}
.banner{border-radius:var(--radius);padding:12px 16px;margin-bottom:16px;border:1px solid var(--line);background:var(--panel2)}
.banner.warn{border-color:#5a4520;background:#2a2213}
.banner.bad{border-color:#5a2525;background:#2a1515}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input[type=text],input[type=password],input[type=number],input[type=date],select,textarea{
  width:100%;padding:9px 11px;border-radius:8px;border:1px solid var(--line);background:#0c0f13;color:var(--text);font:inherit}
textarea{min-height:70px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.check{display:flex;align-items:center;gap:8px;margin:8px 0;color:var(--text);font-size:14px}
button,.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel2);color:var(--text);font:inherit;font-size:14px;cursor:pointer;text-decoration:none}
button:hover,.btn:hover{border-color:#3a4350}
.primary{background:var(--accent);border-color:var(--accent);color:#16120a;font-weight:600}
.primary:hover{filter:brightness(1.05)}
.small{padding:5px 10px;font-size:13px}
.danger{color:var(--bad)}
details summary{cursor:pointer;color:var(--info);margin-top:10px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-weight:500;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
.title{font-weight:600}
.muted{color:var(--muted)}
.stars{color:var(--good);letter-spacing:1px;white-space:nowrap}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.b-synced{background:#123826;color:var(--good)}.b-preview{background:#16283f;color:var(--info)}
.b-waiting{background:#2a2414;color:var(--warn)}.b-skipped{background:#22262c;color:var(--muted)}
.b-failed{background:#3a1717;color:var(--bad)}
.tag{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:6px;font-size:11px;background:#22262c;color:var(--muted)}
.review{max-width:340px;color:#c9d1db;white-space:pre-wrap;overflow-wrap:anywhere}
.err{color:var(--bad);font-size:12px;margin-top:4px;max-width:340px}
.actions{display:flex;gap:6px;flex-wrap:wrap}
.actions form{margin:0}
code,pre{background:#0c0f13;border:1px solid var(--line);border-radius:6px;padding:1px 5px;font-size:13px}
pre{padding:12px;overflow:auto;white-space:pre-wrap}
footer{margin-top:24px;color:var(--muted);font-size:13px;display:flex;gap:16px;flex-wrap:wrap}
@media (max-width:640px){.wrap{padding:16px}td,th{padding:8px 6px}}
"""


def stars(rating: int | None) -> str:
    if not rating:
        return '<span class="muted">-</span>'
    full, half = divmod(int(rating), 2)
    return f'<span class="stars">{"★" * full}{"½" if half else ""}</span>'


def when(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts)).strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def ago(ts: int | None) -> str:
    if not ts:
        return "never"
    import time
    s = int(time.time()) - int(ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def page(body: str, title: str = "Plexboxd") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>"""


def pill(label: str, status: dict | None, ok_text: str = "Connected", none_text: str = "Not connected") -> str:
    if status is None:
        return f'<span class="pill"><span class="dot"></span>{escape(label)}: {escape(none_text)}</span>'
    cls = "ok" if status.get("ok") else "bad"
    text = ok_text if status.get("ok") else "Needs attention"
    tip = escape(status.get("message", ""))
    return f'<span class="pill" title="{tip}"><span class="dot {cls}"></span>{escape(label)}: {text}</span>'


def header(ctx: dict) -> str:
    mode = ('<span class="pill"><span class="dot warn"></span>Preview mode</span>' if ctx["settings"]["preview_mode"]
            else '<span class="pill"><span class="dot ok"></span>Auto-posting</span>')
    plex = pill("Plex", ctx.get("plex_status") if ctx.get("plex_connected") else None)
    lb_status = ctx.get("lb_status") if ctx.get("lb_username") else None
    lb = pill("Letterboxd", lb_status or ({"ok": True} if ctx.get("lb_username") else None),
              ok_text=f'@{escape(ctx.get("lb_username") or "")}')
    rv = ctx.get("review_api_status")
    reviews = pill("Plex reviews", rv, ok_text="Working", none_text="Not checked yet")
    return f"""<header><div class="brand"><div class="logo">P</div>Plexboxd</div>
<div class="pills">{plex}{lb}{reviews}{mode}</div></header>"""


def connect_cards(ctx: dict) -> str:
    cards = []
    if not ctx.get("plex_connected"):
        cards.append("""<div class="card"><h2>1. Connect Plex</h2>
<p>Sign in on plex.tv. Plexboxd gets a token for your account; your Plex password never touches it.</p>
<form method="post" action="/plex/start"><button class="primary">Sign in with Plex</button></form></div>""")
    elif not ctx.get("plex_server_url"):
        cards.append("""<div class="card"><h2>1. Pick your Plex server</h2>
<p>Choose which server's watch history to follow.</p><a class="btn primary" href="/setup/server">Choose server</a></div>""")
    if not ctx.get("lb_username"):
        cards.append(letterboxd_card(ctx))
    return f'<div class="grid">{"".join(cards)}</div>' if cards else ""


def letterboxd_card(ctx: dict) -> str:
    if not ctx.get("lb_configured"):
        return f"""<div class="card"><h2>2. Connect Letterboxd</h2>
<p>Plexboxd uses the official Letterboxd API, which needs <strong>your own</strong> API client ID and secret from Letterboxd.</p>
<ol class="muted" style="margin:0 0 12px;padding-left:18px">
<li>Request API access by emailing <code>api@letterboxd.com</code>. When asked for a redirect URL, give
<code>{escape(ctx.get("lb_redirect_hint") or "")}</code>.</li>
<li>Put the client ID and secret into <code>LETTERBOXD_CLIENT_ID</code> and <code>LETTERBOXD_CLIENT_SECRET</code> in
your <code>.env</code> file.</li>
<li>Restart the container: <code>docker compose up -d</code>.</li></ol>
<a class="btn" href="https://github.com/RamenNuudlss/PlexBoxd#getting-your-own-letterboxd-api-access" target="_blank" rel="noopener">How to request access</a></div>"""
    return """<div class="card"><h2>2. Connect Letterboxd</h2>
<p>You'll sign in on Letterboxd's own page and approve access. Plexboxd never sees your Letterboxd password.</p>
<form method="post" action="/letterboxd/start"><button class="primary">Sign in with Letterboxd</button></form></div>"""


def banners(ctx: dict) -> str:
    out = []
    if ctx.get("flash"):
        kind, msg = ctx["flash"]
        out.append(f'<div class="banner {escape(kind)}">{escape(msg)}</div>')
    lb = ctx.get("lb_status")
    if ctx.get("lb_username") and lb and not lb.get("ok"):
        out.append(f'<div class="banner bad"><strong>Letterboxd needs you to sign in again.</strong> {escape(lb.get("message",""))} '
                   f'Watches are held, not lost.<form method="post" action="/letterboxd/start" style="display:inline;margin-left:8px">'
                   f'<button class="small">Sign in with Letterboxd</button></form></div>')
    pl = ctx.get("plex_status")
    if ctx.get("plex_connected") and pl and not pl.get("ok"):
        out.append(f'<div class="banner bad"><strong>Plex problem:</strong> {escape(pl.get("message",""))}</div>')
    rv = ctx.get("review_api_status")
    if rv and not rv.get("ok"):
        out.append('<div class="banner warn"><strong>Plex reviews couldn\'t be read</strong>, so entries are logging with '
                   f'watch date and rating only. {escape(rv.get("message",""))} See <a href="/doctor">diagnostics</a> and the '
                   'README section "If reviews stop syncing".</div>')
    if ctx["settings"]["preview_mode"] and ctx.get("plex_server_url") and ctx.get("lb_username"):
        out.append('<div class="banner">Preview mode is on: entries wait here for you to check them and press <strong>Post</strong>. '
                   'Once matches look right, turn it off in Settings and everything posts automatically.</div>')
    return "".join(out)


def watch_rows(watches: list[dict]) -> str:
    if not watches:
        return ('<tr><td colspan="6" class="muted" style="padding:24px 10px">No movie watches yet. '
                'Finish a movie on Plex and it will show up here on the next check.</td></tr>')
    rows = []
    for w in watches:
        title = escape(w.get("title") or "Unknown")
        year = f' <span class="muted">({w["year"]})</span>' if w.get("year") else ""
        tags = '<span class="tag">rewatch</span>' if w.get("rewatch") else ""
        if w.get("spoilers"):
            tags += '<span class="tag">spoilers</span>'
        link = ""
        if w.get("lb_slug"):
            link = f' <a class="muted" href="https://letterboxd.com/film/{escape(w["lb_slug"])}/" target="_blank" rel="noopener">&#8599;</a>'
        review = w.get("review") or ""
        review_html = f'<div class="review">{escape(review[:280])}{"&hellip;" if len(review) > 280 else ""}</div>' if review \
            else '<span class="muted">-</span>'
        err = f'<div class="err">{escape(w["error"])}</div>' if w.get("error") else ""
        status = w["status"]
        label = {"waiting": "Waiting", "preview": "Ready", "synced": "Logged", "skipped": "Skipped",
                 "failed": "Failed"}.get(status, status)
        due = f'<div class="muted" style="font-size:12px">checks {escape(when(w["due_at"]))}</div>' \
            if status == "waiting" else ""
        actions = []
        if status in ("preview", "failed", "waiting"):
            actions.append(f'<form method="post" action="/watch/{w["id"]}/post"><button class="small primary">Post</button></form>')
        if status in ("failed", "skipped"):
            actions.append(f'<form method="post" action="/watch/{w["id"]}/retry"><button class="small">Retry</button></form>')
        if status in ("preview", "waiting", "failed"):
            actions.append(f'<form method="post" action="/watch/{w["id"]}/skip"><button class="small">Skip</button></form>')
        rows.append(f"""<tr><td><div class="title">{title}{year}{link}</div>{tags}</td>
<td class="muted" style="white-space:nowrap">{escape(when(w["viewed_at"]))}</td>
<td>{stars(w.get("rating"))}</td><td>{review_html}</td>
<td><span class="badge b-{escape(status)}">{label}</span>{due}{err}</td>
<td><div class="actions">{"".join(actions)}</div></td></tr>""")
    return "".join(rows)


def settings_card(s: dict[str, Any], sync_from: int | None) -> str:
    def chk(name: str, label: str) -> str:
        return f'<label class="check"><input type="checkbox" name="{name}" value="1" {"checked" if s[name] else ""}> {label}</label>'
    sf = datetime.fromtimestamp(sync_from).strftime("%Y-%m-%d") if sync_from else ""
    return f"""<div class="card"><h2>Settings</h2>
<form method="post" action="/settings">
{chk("preview_mode", "Preview mode (hold entries until I press Post)")}
{chk("sync_ratings", "Sync star ratings")}
{chk("sync_reviews", "Sync written reviews")}
{chk("sync_rewatches", "Mark repeat watches as rewatches")}
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin:6px 0 0">
<div><label>Wait after a watch (minutes)</label><input type="number" min="0" name="grace_minutes" value="{s["grace_minutes"]}"></div>
<div><label>Check Plex every (seconds)</label><input type="number" min="30" name="poll_seconds" value="{s["poll_seconds"]}"></div>
<div><label>Pick up late reviews for (days)</label><input type="number" min="0" name="late_review_days" value="{s["late_review_days"]}"></div>
<div><label>Plex account ID</label><input type="number" min="1" name="plex_account_id" value="{s["plex_account_id"]}"></div>
<div><label>Only sync watches from</label><input type="date" name="sync_from" value="{sf}"></div>
</div>
<p class="muted" style="margin-top:10px;font-size:13px">The wait gives you time to rate and review on Plex before the entry is logged.
Account ID 1 is the server owner. Moving "only sync from" earlier backfills older watches (check them in preview mode first).</p>
<div class="row"><button class="primary">Save settings</button></div></form></div>"""


def dashboard(ctx: dict) -> str:
    counts = ctx.get("counts", {})
    last = ctx.get("last_round") or {}
    summary = " · ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing yet"
    body = header(ctx) + banners(ctx) + connect_cards(ctx)
    body += f"""<div class="card" style="margin-bottom:16px">
<div class="row" style="justify-content:space-between">
<div><h2 style="margin:0">Recent movie watches</h2>
<div class="muted" style="font-size:13px">Last check {escape(ago(last.get("at")))}: {escape(last.get("message","-"))} · {escape(summary)}</div></div>
<form method="post" action="/sync-now"><button>Check now</button></form></div>
<div class="table-wrap" style="margin-top:12px"><table>
<thead><tr><th>Movie</th><th>Watched</th><th>Rating</th><th>Review</th><th>Status</th><th></th></tr></thead>
<tbody>{watch_rows(ctx.get("watches", []))}</tbody></table></div></div>"""
    body += '<div class="grid">' + settings_card(ctx["settings"], ctx.get("sync_from"))
    body += f"""<div class="card"><h2>Accounts</h2>
<p>Plex: {escape(ctx.get("plex_username") or "not connected")}{" on " + escape(ctx["plex_server_name"]) if ctx.get("plex_server_name") else ""}<br>
Letterboxd: {"@" + escape(ctx["lb_username"]) if ctx.get("lb_username") else "not connected"}</p>
<div class="row">
<a class="btn" href="/setup/server">Change Plex server</a>
<form method="post" action="/plex/start"><button>Re-sign in to Plex</button></form>
<form method="post" action="/letterboxd/start"><button>Re-sign in to Letterboxd</button></form>
<form method="post" action="/letterboxd/logout"><button class="danger">Sign out of Letterboxd</button></form>
</div></div></div>"""
    body += '<footer><a href="/doctor">Diagnostics</a><span>Plexboxd</span></footer>'
    return page(body)


def server_picker(servers: list[dict], current: str | None, error: str | None = None) -> str:
    opts = []
    for s in servers:
        for c in s["connections"]:
            uri = c["uri"]
            sel = "checked" if uri == current else ""
            where = "local" if c["local"] else "remote"
            opts.append(f'<label class="check"><input type="radio" name="choice" value="{escape(s["name"])}|{escape(uri)}" {sel}> '
                        f'<strong>{escape(s["name"])}</strong> <span class="muted">{escape(uri)} ({where})</span></label>')
    listing = "".join(opts) or '<p class="muted">No servers you own were found on this Plex account.</p>'
    err = f'<div class="banner bad">{escape(error)}</div>' if error else ""
    return page(f"""<header><div class="brand"><div class="logo">P</div>Plexboxd</div></header>{err}
<div class="card"><h2>Choose your Plex server</h2>
<p>Pick a local address if Plexboxd runs on the same network as your server (it does on your NAS).</p>
<form method="post" action="/setup/server">{listing}
<label>Or enter the address yourself</label><input type="text" name="manual" placeholder="http://192.168.1.50:32400">
<div class="row" style="margin-top:14px"><button class="primary">Use this server</button><a class="btn" href="/">Cancel</a></div>
</form></div>""", "Choose server")


def doctor_page(report: list[tuple[str, bool | None, str]]) -> str:
    rows = "".join(
        f'<tr><td>{escape(name)}</td><td><span class="badge {"b-synced" if ok else ("b-failed" if ok is False else "b-skipped")}">'
        f'{"OK" if ok else ("Problem" if ok is False else "Info")}</span></td><td><pre>{escape(detail)}</pre></td></tr>'
        for name, ok, detail in report)
    return page(f"""<header><div class="brand"><div class="logo">P</div>Plexboxd</div></header>
<div class="card"><h2>Diagnostics</h2><p>Live checks against Plex and Letterboxd with your current sign-ins.</p>
<div class="table-wrap"><table><thead><tr><th>Check</th><th></th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="row" style="margin-top:12px"><a class="btn" href="/">Back</a><a class="btn" href="/doctor">Run again</a></div></div>""",
                "Diagnostics")
