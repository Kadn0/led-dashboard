#!/usr/bin/env python3
import os, json, time, functools, secrets as _secrets, base64 as _b64
from io import BytesIO
from pathlib import Path
from urllib.parse import quote as _urlquote
from flask import Flask, request, redirect, jsonify, render_template_string, send_from_directory, abort, session, send_file
import requests as _req

from dashboard_config import (
    WEB_PORT, WEB_PASSWORD as PASSWORD, WEB_TITLE,
    LOCATION_LAT, LOCATION_LON,
    PHOTOS_DIR, CACHE_DIR,
    MANUAL_TRACK_FILE, OVERRIDE_FILE, STATUS_FILE,
    PHOTO_SETTINGS_FILE, BRIGHT_SCHEDULE_FILE, PID_FILE,
    DEFAULT_BRIGHT_SCHEDULE as _DEFAULT_SCHEDULE,
    CLOCK_SETTINGS_FILE, CLOCK_TIMEZONES,
)

app = Flask(__name__)
app.secret_key = "cf82a1d9e4b7f305c6182a3d"

_DEFAULT_SEGMENTS = [
    {"end": 6,  "bright": 0},
    {"end": 10, "bright": 35},
    {"end": 20, "bright": 100},
    {"end": 23, "bright": 35},
    {"bright": 0},
]

def get_bright_schedule():
    """Returns {"segments": [{bright, end?}, ...]} — last entry has no 'end' (→ 24:00).
    Reads the new flexible format, or converts the legacy 4-key format on the fly."""
    try:
        if os.path.exists(BRIGHT_SCHEDULE_FILE):
            data = json.loads(open(BRIGHT_SCHEDULE_FILE).read())
            if isinstance(data.get("segments"), list) and data["segments"]:
                return data
            s = {**_DEFAULT_SCHEDULE, **data}
            return {"segments": [
                {"end": float(s["night_end"]),   "bright": int(s["night_bright"])},
                {"end": float(s["morning_end"]), "bright": int(s["morning_bright"])},
                {"end": float(s["day_end"]),     "bright": int(s["day_bright"])},
                {"end": float(s["evening_end"]), "bright": int(s["evening_bright"])},
                {"bright": int(s["night_bright"])},
            ]}
    except Exception:
        pass
    return {"segments": list(_DEFAULT_SEGMENTS)}

# ── Clock settings (single Eastern vs 4-zone grid) ───────────────────────
_DEFAULT_CLOCK_ZONES = [[l, tz] for (l, tz, _c) in CLOCK_TIMEZONES][:4]
# Curated picker list: (display name, short on-screen label, IANA tz).
# The short label is what shows in the 32px grid cell.
CLOCK_TZ_OPTIONS = [
    ("Eastern (New York)", "EST", "America/New_York"),
    ("Central (Chicago)",  "CST", "America/Chicago"),
    ("Mountain (Denver)",  "MST", "America/Denver"),
    ("Pacific (Los Angeles)", "PST", "America/Los_Angeles"),
    ("Alaska (Anchorage)", "AK",  "America/Anchorage"),
    ("Hawaii (Honolulu)",  "HI",  "Pacific/Honolulu"),
    ("UTC",                "UTC", "UTC"),
    ("London",             "LON", "Europe/London"),
    ("Paris",              "PAR", "Europe/Paris"),
    ("Berlin",             "BER", "Europe/Berlin"),
    ("Moscow",             "MOW", "Europe/Moscow"),
    ("Dubai",              "DXB", "Asia/Dubai"),
    ("India (Kolkata)",    "IND", "Asia/Kolkata"),
    ("China (Shanghai)",   "CHN", "Asia/Shanghai"),
    ("Tokyo",              "TYO", "Asia/Tokyo"),
    ("Sydney",             "SYD", "Australia/Sydney"),
    ("Sao Paulo",          "SAO", "America/Sao_Paulo"),
    ("Mexico City",        "MEX", "America/Mexico_City"),
]
_VALID_TZS = {tz for (_n, _l, tz) in CLOCK_TZ_OPTIONS}

def get_clock_settings():
    """Returns {"mode": "single"|"quad", "zones": [[label, tz], ...4]}."""
    result = {"mode": "single", "zones": [list(z) for z in _DEFAULT_CLOCK_ZONES]}
    try:
        if os.path.exists(CLOCK_SETTINGS_FILE):
            data = json.loads(open(CLOCK_SETTINGS_FILE).read())
            if data.get("mode") in ("single", "quad"):
                result["mode"] = data["mode"]
            z = data.get("zones")
            if isinstance(z, list) and z:
                zones = [[str(e[0])[:4], str(e[1])] for e in z[:4]
                         if isinstance(e, (list, tuple)) and len(e) >= 2]
                if zones:
                    result["zones"] = zones
    except Exception:
        pass
    return result

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
ART_PREFIXES      = ("homepod_", "spotify_", "album_", "art_")

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{{ title }} – Login</title>
<style>
:root { --bg:#08090c; --surface:#111318; --border:#252830; --blue:#3b82f6; }
* { box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }
body { font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; background:var(--bg); color:#e8eaf0; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:20px; padding:32px 28px; width:100%; max-width:360px; }
.icon { font-size:40px; text-align:center; margin-bottom:12px; }
h1 { font-size:22px; font-weight:700; text-align:center; margin-bottom:4px; }
.sub { font-size:13px; color:#6b7280; text-align:center; margin-bottom:28px; }
label { font-size:12px; color:#6b7280; display:block; margin-bottom:6px; }
input[type=password] { width:100%; padding:14px; font-size:17px; background:#1a1d25; border:1px solid var(--border); border-radius:12px; color:#e8eaf0; outline:none; -webkit-appearance:none; margin-bottom:14px; transition:border-color .15s; }
input[type=password]:focus { border-color:var(--blue); }
button { width:100%; padding:14px; font-size:16px; font-weight:600; background:var(--blue); color:#fff; border:none; border-radius:12px; cursor:pointer; }
button:active { opacity:.8; }
.error { background:#3b0f0f; border:1px solid #7f1d1d; color:#fca5a5; border-radius:10px; padding:11px 14px; font-size:14px; margin-bottom:14px; text-align:center; }
</style>
</head>
<body>
<div class="card">
  <div class="icon">💡</div>
  <h1>{{ title }}</h1>
  <p class="sub">Enter password to continue</p>
  {% if error %}<div class="error">Incorrect password</div>{% endif %}
  <form method="post" action="/login">
    <label>Password</label>
    <input type="password" name="password" autofocus autocomplete="current-password" placeholder="••••••••">
    <button type="submit">Unlock</button>
  </form>
</div>
</body>
</html>"""

def get_manual_track():
    try:
        if os.path.exists(MANUAL_TRACK_FILE):
            return json.loads(open(MANUAL_TRACK_FILE).read())
    except Exception:
        pass
    return {"callsign": None, "status": "idle"}

def set_manual_track(callsign):
    with open(MANUAL_TRACK_FILE, "w") as f:
        json.dump({"callsign": callsign.upper().strip() if callsign else None,
                   "status": "searching" if callsign else "idle"}, f)

def get_override():
    try:
        if os.path.exists(OVERRIDE_FILE):
            return json.loads(open(OVERRIDE_FILE).read())
    except Exception:
        pass
    return {"brightness": None, "slide_lock": None}

def set_override(**kwargs):
    ov = get_override()
    for k, v in kwargs.items():
        if v is None:
            ov.pop(k, None)
        else:
            ov[k] = v
    # Atomic write — swap in a temp file so a mid-write crash can't corrupt the JSON
    tmp = OVERRIDE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ov, f)
    os.replace(tmp, OVERRIDE_FILE)

def list_photos():
    try:
        return [f for f in sorted(os.listdir(PHOTOS_DIR))
                if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS]
    except Exception:
        return []

def get_photo_settings():
    try:
        with open(PHOTO_SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_photo_settings(settings):
    with open(PHOTO_SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

def get_art_cache_info():
    try:
        files = [f for f in os.listdir(CACHE_DIR) if any(f.startswith(p) for p in ART_PREFIXES)]
        total = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in files)
        return {"files": len(files), "mb": round(total / 1024 / 1024, 1)}
    except Exception:
        return {"files": 0, "mb": 0}

def get_dashboard_pid():
    try:
        if os.path.exists(PID_FILE):
            pid = int(open(PID_FILE).read().strip())
            os.kill(pid, 0)
            return pid
    except (ProcessLookupError, ValueError, PermissionError):
        pass
    return None

def get_cpu_temp():
    try:
        raw = open("/sys/class/thermal/thermal_zone0/temp").read().strip()
        return round(int(raw) / 1000.0, 1)
    except Exception:
        return None

def get_live_data():
    try:
        if os.path.exists(STATUS_FILE):
            return json.loads(open(STATUS_FILE).read())
    except Exception:
        pass
    return {"updated": 0, "music": None, "planes": [], "iss": None, "weather": None}

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>{{ title }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
/* Leaflet overrides for dark UI */
.leaflet-container { background:#010306; font-family:'Share Tech Mono',monospace; }
.leaflet-control-zoom a { background:var(--panel)!important; color:var(--text)!important; border-color:var(--border)!important; }
.leaflet-control-zoom a:hover { background:var(--surface)!important; }
.leaflet-bar { border:1px solid var(--border)!important; box-shadow:none!important; }
.plane-label { background:none; border:none; box-shadow:none; overflow:visible !important; }
.plane-label > div { pointer-events:none; }
/* Tooltip */
.leaflet-tooltip.plane-tooltip { background:rgba(5,10,20,.92); border:1px solid rgba(0,180,255,.35); color:#a8d4f5; font-family:'Share Tech Mono',monospace; font-size:11px; padding:4px 8px; border-radius:5px; box-shadow:0 2px 8px rgba(0,0,0,.6); white-space:nowrap; }
.leaflet-tooltip.plane-tooltip::before { border-top-color:rgba(0,180,255,.35); }
.plane-popup-leaflet .leaflet-popup-content-wrapper { background:#0d1117; border:1px solid rgba(59,130,246,.45); border-radius:14px; box-shadow:0 12px 40px rgba(0,0,0,.7); padding:0; color:#e8eaf0; }
.plane-popup-leaflet .leaflet-popup-content { margin:0; padding:14px 16px; min-width:220px; }
.plane-popup-leaflet .leaflet-popup-tip-container { display:none; }
.plane-popup-leaflet .leaflet-popup-close-button { color:#6b7280!important; top:8px!important; right:10px!important; font-size:18px!important; }
:root {
  --bg:#010306; --panel:#080c12; --surface:#0e1420; --border:#182030;
  --text:#d0d8f0; --muted:#3a4460; --dim:#1e2840;
  --red:#ff2244; --green:#00ff88; --blue:#2288ff; --cyan:#00ccff; --amber:#ffaa00; --white:#e8f0ff;
  --gap:10px; --pad:10px; --r:8px;
}
@media(min-width:480px)  { :root { --gap:12px; --pad:14px; } }
@media(min-width:768px)  { :root { --gap:14px; --pad:18px; } }
@media(min-width:1024px) { :root { --gap:18px; --pad:22px; } }
@media(min-width:1400px) { :root { --gap:22px; --pad:28px; } }

* { box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }
body {
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  background-color:var(--bg);
  background-image:radial-gradient(circle,#0d1520 1.2px,transparent 1.2px);
  background-size:5px 5px; color:var(--text); min-height:100vh;
  padding:env(safe-area-inset-top,0) 0 env(safe-area-inset-bottom,48px);
}

/* ── Page wrapper ── */
.page-wrap { width:100%; }

/* ── Header ── */
.header { display:flex; align-items:center; justify-content:space-between; padding:16px var(--pad) 12px; }
.header-title { font-family:'Share Tech Mono',monospace; font-size:clamp(16px,4vw,22px); letter-spacing:clamp(1px,.5vw,3px); text-transform:uppercase; color:var(--red); text-shadow:0 0 6px rgba(255,34,68,.4); }
.header-sub { font-size:clamp(9px,1.8vw,11px); color:var(--muted); margin-top:4px; letter-spacing:1px; text-transform:uppercase; }
.live-badge { display:flex; align-items:center; gap:6px; background:rgba(0,255,136,.06); border:1px solid rgba(0,255,136,.2); border-radius:6px; padding:5px 10px; font-family:'Share Tech Mono',monospace; font-size:clamp(10px,2vw,12px); letter-spacing:1.5px; color:var(--green); white-space:nowrap; }
.live-dot { width:7px; height:7px; flex-shrink:0; border-radius:50%; background:var(--green); box-shadow:0 0 5px var(--green); animation:pulse 2s ease-in-out infinite; }
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ── Dashboard grid — mobile-first 1-col → 2-col → 3-col ── */
.dash-grid {
  display:grid;
  grid-template-columns:1fr;
  grid-auto-rows:auto;
  align-items:start;
  gap:var(--gap);
  padding:0 var(--pad) 60px;
}
.dash-full  { grid-column:1; }

/* Each section stretches to fill its grid cell */
.section             { margin:0; display:flex; flex-direction:column; }
.section > .card     { flex:1; min-height:0; }

/* dash-stack fills its cell and splits the two cards evenly */
.dash-stack          { display:flex; flex-direction:column; gap:var(--gap); }
.dash-stack .section { flex:1; }

@media(min-width:560px) {
  .dash-grid  { grid-template-columns:1fr 1fr; }
  .dash-full  { grid-column:1/-1; }
}
@media(min-width:900px) {
  .dash-grid  { grid-template-columns:1fr 1fr 1fr; }
}

/* ── Radar map ── */
#radarMap { border-radius:8px 8px 0 0; }

/* ── Section titles ── */
.section-title { font-family:'Share Tech Mono',monospace; font-size:12px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); padding:0 2px 0 10px; margin-bottom:8px; border-left:2px solid rgba(34,136,255,.4); }

/* ── Cards ── */
.card { background:var(--panel); border:1px solid var(--border); border-radius:var(--r); padding:clamp(12px,2.5vw,20px); position:relative; box-shadow:inset 0 1px 0 rgba(255,255,255,.03),0 2px 16px rgba(0,0,0,.6); transition:border-color .2s; }
.card:hover { border-color:rgba(34,136,255,.25); }
.card+.card { margin-top:var(--gap); }

/* ── Buttons ── */
button { display:inline-flex; align-items:center; justify-content:center; gap:7px; padding:11px 14px; font-size:clamp(12px,2vw,14px); font-weight:600; border:1px solid var(--border); border-radius:6px; cursor:pointer; transition:all .15s; width:100%; background:var(--surface); color:var(--text); -webkit-appearance:none; letter-spacing:.3px; }
button:active { transform:scale(.96); opacity:.8; }
.btn-primary { background:rgba(34,136,255,.15); color:var(--blue); border-color:rgba(34,136,255,.35); }
.btn-danger  { background:rgba(255,34,68,.1); color:#ff6688; border-color:rgba(255,34,68,.3); }
.btn-success { background:rgba(0,255,136,.08); color:var(--green); border-color:rgba(0,255,136,.25); }
.btn-ghost   { background:var(--surface); color:var(--text); border-color:var(--border); }
.btn-active  { background:rgba(34,136,255,.18)!important; color:var(--cyan)!important; border-color:var(--cyan)!important; box-shadow:inset 0 0 8px rgba(0,204,255,.06)!important; }
.btn-ghost.active { background:rgba(34,136,255,.18); color:var(--cyan); border-color:var(--cyan); box-shadow:inset 0 0 8px rgba(0,204,255,.06); }
.clk-tz { background:var(--surface); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:8px; font-size:12px; font-family:inherit; width:100%; }
.btn-icon    { width:auto; padding:7px 10px; font-size:13px; border-radius:5px; }
.row { display:flex; gap:6px; flex-wrap:wrap; }
.row button { flex:1; min-width:60px; }

/* ── Brightness ── */
.bright-val { font-family:'Share Tech Mono',monospace; font-size:clamp(36px,8vw,56px); font-weight:400; text-align:center; color:var(--amber); letter-spacing:2px; text-shadow:0 0 10px rgba(255,170,0,.35); margin-bottom:2px; line-height:1; }
.bright-auto-tag { font-family:'Share Tech Mono',monospace; font-size:12px; color:var(--muted); text-align:center; margin-bottom:14px; letter-spacing:2px; text-transform:uppercase; }
input[type=range] { width:100%; height:4px; -webkit-appearance:none; appearance:none; background:var(--border); border-radius:2px; outline:none; margin:6px 0 18px; }
input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:20px; height:20px; border-radius:50%; background:var(--amber); cursor:pointer; }

/* ── Slideshow grid — 2-col mobile, 3-col 400px+ ── */
.slide-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px; }
@media(min-width:400px) { .slide-grid { grid-template-columns:1fr 1fr 1fr; } }
.slide-item { position:relative; display:block; }
.slide-btn { position:relative; z-index:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:6px; padding:clamp(10px,2vw,16px) 6px; font-size:11px; font-weight:500; letter-spacing:.5px; background:var(--surface); color:var(--muted); border:1px solid var(--border); border-radius:6px; cursor:pointer; transition:all .15s; width:100%; -webkit-appearance:none; }
.slide-btn svg { opacity:.45; transition:opacity .15s; }
.slide-btn:active { transform:scale(.95); }
.slide-btn.btn-active { background:rgba(34,136,255,.14)!important; color:var(--cyan)!important; border-color:rgba(0,204,255,.4)!important; box-shadow:inset 0 0 8px rgba(0,204,255,.05)!important; }
.slide-btn.btn-active svg { opacity:1; }
.slide-btn.slide-off { opacity:.3; }
.slide-power { position:absolute; top:5px; right:5px; width:22px; height:22px; border-radius:5px; background:rgba(0,0,0,.5); border:1px solid rgba(255,255,255,.08); color:var(--muted); cursor:pointer; display:flex; align-items:center; justify-content:center; padding:0; transition:all .15s; z-index:10; }
.slide-power:hover { background:rgba(255,68,102,.2); color:#ff8899; border-color:rgba(255,68,102,.4); }
.slide-power.slide-off { background:rgba(255,34,68,.2); color:#ff4466; border-color:rgba(255,68,102,.5); box-shadow:0 0 6px rgba(255,34,68,.3); }

/* ── Flight cards ── */
.flight-card { border-radius:6px; padding:14px 16px; margin-bottom:12px; border:1px solid var(--border); background:var(--surface); }
.flight-card.state-tracking  { border-color:rgba(34,136,255,.5); background:rgba(34,136,255,.07); }
.flight-card.state-searching { border-color:rgba(255,170,0,.4); background:rgba(255,170,0,.05); }
.flight-card.state-notfound  { border-color:rgba(255,34,68,.4); background:rgba(255,34,68,.05); }
.flight-badge { display:inline-flex; align-items:center; gap:5px; font-family:'Share Tech Mono',monospace; font-size:12px; letter-spacing:1.5px; text-transform:uppercase; padding:3px 8px; border-radius:4px; margin-bottom:10px; }
.badge-tracking  { background:rgba(34,136,255,.15); color:var(--cyan); border:1px solid rgba(0,204,255,.3); text-shadow:0 0 6px var(--cyan); }
.badge-searching { background:rgba(255,170,0,.12); color:var(--amber); border:1px solid rgba(255,170,0,.3); }
.badge-notfound  { background:rgba(255,34,68,.12); color:#ff6688; border:1px solid rgba(255,34,68,.3); }
.badge-idle      { background:var(--surface); color:var(--muted); border:1px solid var(--border); }
.callsign-big { font-family:'Share Tech Mono',monospace; font-size:clamp(22px,5vw,34px); letter-spacing:clamp(2px,.8vw,5px); color:var(--white); text-shadow:0 0 10px rgba(255,255,255,.3); }
.flight-sub { font-size:14px; color:var(--muted); margin-top:5px; letter-spacing:.5px; }
.dot-anim::after { content:'.'; animation:dots 1.2s steps(3,end) infinite; }
@keyframes dots{0%{content:'.'}33%{content:'..'}66%{content:'...'}}

/* ── Radar ── */
.radar-empty { color:var(--muted); font-size:14px; text-align:center; padding:14px 0; letter-spacing:.5px; font-family:'Share Tech Mono',monospace; }
.radar-count { font-family:'Share Tech Mono',monospace; font-size:13px; color:var(--muted); letter-spacing:1.5px; text-transform:uppercase; margin-bottom:10px; }
.radar-count span { color:var(--cyan); text-shadow:0 0 6px var(--cyan); }
.radar-row { display:flex; align-items:center; padding:6px 0; border-bottom:1px solid rgba(255,255,255,.04); gap:8px; font-family:'Share Tech Mono',monospace; font-size:13px; }
.radar-row:last-child { border-bottom:none; padding-bottom:0; }
.radar-cs   { font-family:'Share Tech Mono',monospace; font-size:15px; color:var(--white); letter-spacing:1px; min-width:80px; text-decoration:none; cursor:pointer; }
.radar-cs:hover { color:var(--cyan); text-shadow:0 0 8px rgba(0,204,255,.5); text-decoration:underline; }
.radar-dist { font-family:'Share Tech Mono',monospace; font-size:14px; color:var(--amber); text-shadow:0 0 6px rgba(255,170,0,.4); min-width:48px; }
.radar-alt  { font-size:13px; color:var(--muted); min-width:60px; }
.radar-route{ font-size:13px; color:var(--cyan); opacity:.8; flex:1; text-align:right; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
/* ── Flight hover popup ── */
#flightPopup { position:fixed; z-index:9999; min-width:240px; max-width:300px; background:#0d1117; border:1px solid rgba(59,130,246,.5); border-radius:14px; padding:14px 16px; box-shadow:0 12px 40px rgba(0,0,0,.7),0 0 0 1px rgba(59,130,246,.15); pointer-events:none; display:none; font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; }
.fp-header { display:flex; align-items:center; gap:12px; margin-bottom:10px; }
.fp-logo { width:44px; height:44px; object-fit:contain; border-radius:6px; background:rgba(255,255,255,.06); padding:4px; flex-shrink:0; }
.fp-cs { font-family:'Share Tech Mono',monospace; font-size:20px; font-weight:700; color:var(--white); letter-spacing:1.5px; line-height:1.1; }
.fp-airline { font-size:12px; color:var(--muted); margin-top:2px; }
.fp-divider { border:none; border-top:1px solid rgba(255,255,255,.06); margin:8px 0; }
.fp-route { font-family:'Share Tech Mono',monospace; font-size:15px; color:var(--cyan); letter-spacing:1px; }
.fp-cities { font-size:11px; color:var(--muted); margin-top:3px; }
.fp-stats { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
.fp-stat { font-family:'Share Tech Mono',monospace; font-size:12px; color:var(--white); background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.08); border-radius:5px; padding:2px 7px; }
.fp-reg { font-size:12px; color:var(--muted); margin-top:4px; }
.fp-coords { font-family:'Share Tech Mono',monospace; font-size:11px; color:var(--muted); margin-top:4px; letter-spacing:.5px; }
.fp-dist { font-size:12px; color:var(--amber); margin-top:6px; }
.fp-fa { display:block; margin-top:10px; font-size:12px; color:var(--blue); text-decoration:none; pointer-events:all; }
.fp-fa:hover { text-decoration:underline; }

/* ── ISS ── */
.iss-distance { font-family:'Share Tech Mono',monospace; font-size:clamp(26px,5vw,36px); color:var(--white); letter-spacing:2px; text-shadow:0 0 12px rgba(255,255,255,.25); line-height:1; }
.iss-unit { font-size:15px; color:var(--muted); letter-spacing:1px; margin-top:3px; }
.iss-overhead { display:inline-flex; align-items:center; gap:6px; background:rgba(0,255,136,.1); border:1px solid rgba(0,255,136,.3); border-radius:4px; padding:4px 10px; font-family:'Share Tech Mono',monospace; font-size:13px; letter-spacing:1.5px; color:var(--green); text-shadow:0 0 6px var(--green); margin-top:8px; }
.iss-stale { font-size:13px; color:var(--muted); margin-top:6px; letter-spacing:.5px; }

/* ── Inputs ── */
.input-label { font-family:'Share Tech Mono',monospace; font-size:12px; color:var(--muted); margin-bottom:7px; letter-spacing:1.5px; text-transform:uppercase; }
input[type=text] { width:100%; padding:12px 14px; font-size:clamp(16px,3.5vw,20px); font-family:'Share Tech Mono',monospace; letter-spacing:3px; text-transform:uppercase; background:var(--surface); border:1px solid var(--border); border-radius:6px; color:var(--white); -webkit-appearance:none; outline:none; transition:border-color .15s,box-shadow .15s; margin-bottom:10px; text-shadow:0 0 8px rgba(255,255,255,.2); }
input[type=text]:focus { border-color:rgba(34,136,255,.5); box-shadow:0 0 0 1px rgba(34,136,255,.2),0 0 12px rgba(34,136,255,.15); }
input[type=text]::placeholder { color:var(--dim); letter-spacing:1px; font-size:15px; text-transform:none; }

/* ── Upload ── */
.upload-zone { border:1px dashed var(--border); border-radius:6px; padding:20px 12px; text-align:center; color:var(--muted); font-size:14px; margin-bottom:12px; cursor:pointer; position:relative; transition:border-color .15s,background .15s; background:var(--surface); }
.upload-zone input[type=file] { position:absolute; inset:0; opacity:0; cursor:pointer; width:100%; height:100%; }
.upload-zone.active { border-color:rgba(0,255,136,.4); background:rgba(0,255,136,.05); color:var(--green); box-shadow:0 0 12px rgba(0,255,136,.15); }
.upload-icon { font-size:24px; margin-bottom:8px; }
.upload-hint { font-size:13px; color:var(--muted); margin-top:4px; }

/* ── Photos inner layout: stacked mobile, side-by-side 680px+ ── */
.photos-inner { display:grid; grid-template-columns:1fr; gap:14px; align-items:start; }
@media(min-width:680px) { .photos-inner { grid-template-columns:minmax(200px,260px) 1fr; } }

/* ── Photo grid — scales up with available space ── */
.photo-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin-top:10px; }
@media(min-width:480px) { .photo-grid { grid-template-columns:repeat(4,1fr); } }
@media(min-width:768px) { .photo-grid { grid-template-columns:repeat(5,1fr); } }
@media(min-width:1024px){ .photo-grid { grid-template-columns:repeat(6,1fr); gap:8px; } }
@media(min-width:1280px){ .photo-grid { grid-template-columns:repeat(8,1fr); } }
.photo-tile { position:relative; aspect-ratio:1; border-radius:6px; overflow:hidden; background:var(--surface); cursor:pointer; transition:box-shadow .15s; }
.photo-tile img { width:100%; height:100%; object-fit:cover; display:block; transition:opacity .15s; }
.photo-tile:active { opacity:.8; }
.photo-tile:active img { opacity:.7; }
.photo-tile.showing      { box-shadow:0 0 0 2px var(--cyan),0 0 14px rgba(0,204,255,.5); }
.photo-tile.showing-soft { box-shadow:0 0 0 2px var(--green),0 0 14px rgba(0,255,136,.4); }
.photo-delete { position:absolute; top:3px; right:3px; width:22px; height:22px; border-radius:4px; background:rgba(0,0,0,.75); color:#ff6688; border:1px solid rgba(255,34,68,.3); cursor:pointer; font-size:12px; display:flex; align-items:center; justify-content:center; padding:0; backdrop-filter:blur(4px); transition:all .15s; }
.photo-delete:hover { background:rgba(255,34,68,.7); color:#fff; }
.photo-edit { position:absolute; top:3px; right:29px; width:22px; height:22px; border-radius:4px; background:rgba(0,0,0,.75); color:var(--muted); border:1px solid rgba(120,120,180,.3); cursor:pointer; font-size:11px; display:flex; align-items:center; justify-content:center; padding:0; backdrop-filter:blur(4px); transition:all .15s; }
.photo-edit:hover { background:rgba(80,80,200,.5); color:#fff; }
.photo-empty { color:var(--muted); font-size:14px; text-align:center; padding:20px 0; letter-spacing:.5px; }
.photo-count-bar { display:flex; align-items:center; justify-content:space-between; font-size:13px; color:var(--muted); margin-top:6px; }

/* ── Edit popup ── */
.edit-popup { position:fixed; display:none; z-index:300; background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:14px 14px 10px; box-shadow:0 4px 32px rgba(0,0,0,.9); width:200px; }
.edit-popup.open { display:block; }
.edit-popup-title { font-family:'Share Tech Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--muted); text-transform:uppercase; margin-bottom:12px; text-align:center; }
.edit-row { display:flex; align-items:center; justify-content:space-between; margin-bottom:9px; gap:8px; }
.edit-row label { font-size:13px; color:var(--muted); min-width:68px; }
.edit-row input[type=range] { flex:1; accent-color:var(--cyan); height:3px; }
.edit-val { font-size:13px; color:var(--white); min-width:32px; text-align:right; font-family:'Share Tech Mono',monospace; }
.edit-popup-actions { display:flex; gap:7px; margin-top:12px; }
.edit-popup-actions button { flex:1; font-size:13px; padding:6px 4px; border-radius:6px; }

/* ── Modal ── */
.photo-overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none; align-items:center; justify-content:center; z-index:300; backdrop-filter:blur(4px); }
.photo-overlay.open { display:flex; }
.photo-modal { background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:18px 16px 14px; width:min(260px,90vw); box-shadow:0 6px 36px rgba(0,0,0,.8); }
.photo-modal-title { font-family:'Share Tech Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--muted); text-transform:uppercase; margin-bottom:6px; text-align:center; }
.photo-modal-name { font-size:14px; color:var(--white); margin-bottom:14px; text-align:center; word-break:break-all; opacity:.75; }
.photo-modal-actions { display:flex; flex-direction:column; gap:8px; }
.photo-modal-actions button { font-size:14px; padding:9px 4px; border-radius:8px; }
.photo-modal-cancel { background:none; border:none; color:var(--muted); font-size:14px; padding:8px; cursor:pointer; width:100%; margin-top:4px; }

/* ── Forced banner ── */
.forced-banner { display:none; align-items:center; gap:8px; background:rgba(0,204,255,.08); border:1px solid rgba(0,204,255,.25); border-radius:8px; padding:8px 10px; margin-bottom:10px; }
.forced-banner.show { display:flex; }
.forced-banner-dot { width:8px; height:8px; border-radius:50%; background:var(--cyan); flex-shrink:0; animation:pulse 1.5s infinite; }
.forced-banner-dot.soft { background:var(--green); }
.forced-banner-label { flex:1; font-size:13px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.forced-banner-stop { background:none; border:1px solid rgba(255,100,100,.4); color:#ff8899; font-size:13px; padding:4px 10px; border-radius:6px; cursor:pointer; flex-shrink:0; }
.forced-banner-stop:hover { background:rgba(255,34,68,.2); }

/* ── Monitor grid ── */
.monitor-grid { display:grid; grid-template-columns:1fr; gap:8px; }
@media(min-width:380px) { .monitor-grid { grid-template-columns:1fr 1fr; } }
.monitor-card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px; }
.monitor-label { font-family:'Share Tech Mono',monospace; font-size:11px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); margin-bottom:8px; display:flex; align-items:center; gap:6px; }
.monitor-dot { width:6px; height:6px; border-radius:50%; background:var(--muted); flex-shrink:0; }
.monitor-dot.ok   { background:var(--green); box-shadow:0 0 6px var(--green); }
.monitor-dot.warn { background:var(--amber); box-shadow:0 0 6px var(--amber); }
.monitor-dot.err  { background:var(--red); }
.monitor-val { font-family:'Share Tech Mono',monospace; font-size:clamp(16px,3.5vw,20px); color:var(--white); letter-spacing:1px; line-height:1.1; }
.monitor-sub { font-size:13px; color:var(--muted); margin-top:4px; }
.monitor-tag { font-size:12px; color:var(--cyan); margin-top:6px; }
.monitor-stats { display:grid; grid-template-columns:1fr 1fr; gap:3px 10px; margin-top:6px; }
.monitor-stats span { font-size:12px; color:var(--muted); white-space:nowrap; }

/* ── Status ── */
.status-inner { display:grid; grid-template-columns:1fr; gap:var(--gap); align-items:start; }
@media(min-width:480px) { .status-inner { grid-template-columns:1fr 1fr; } }
.status-row { display:flex; align-items:center; justify-content:space-between; padding:10px 0; border-bottom:1px solid var(--border); }
.status-row:last-child { border-bottom:none; padding-bottom:0; }
.status-label { font-family:'Share Tech Mono',monospace; font-size:12px; letter-spacing:2px; color:var(--muted); text-transform:uppercase; }
.status-val { font-family:'Share Tech Mono',monospace; font-size:15px; letter-spacing:1px; }
.status-ok   { color:var(--green); }
.status-warn { color:var(--amber); }
.status-err  { color:#ff6688; }

hr { border:none; border-top:1px solid var(--border); margin:14px 0; }
/* Schedule timeline */
.tl-wrap { padding:4px 0 0; }
.tl-labels { display:flex; align-items:flex-end; height:42px; margin-bottom:5px; }
.tl-label-cell { display:flex; flex-direction:column; align-items:center; justify-content:flex-end; overflow:hidden; min-width:0; transition:width .12s; gap:2px; }
.tl-label-pct { font-family:'Share Tech Mono',monospace; font-size:13px; font-weight:600; color:var(--amber); white-space:nowrap; line-height:1; }
.tl-label-time { font-family:'Share Tech Mono',monospace; font-size:9px; color:var(--muted); white-space:nowrap; line-height:1; letter-spacing:0.1px; }
.tl-bar-wrap { position:relative; height:40px; cursor:crosshair; border-radius:20px; }
.tl-bar { display:flex; height:100%; border-radius:20px; overflow:hidden; }
.tl-seg { height:100%; transition:width .12s,background .2s; }
.tl-handles { position:absolute; inset:0; pointer-events:none; overflow:visible; }
.tl-handle { position:absolute; top:0; bottom:0; width:32px; transform:translateX(-50%); pointer-events:all; cursor:ew-resize; z-index:10; touch-action:none; display:flex; align-items:center; justify-content:center; overflow:visible; }
.tl-handle-dot { position:relative; width:20px; height:20px; border-radius:50%; border:2.5px solid var(--bg); transition:transform .1s,box-shadow .1s; flex-shrink:0; }
.tl-handle:active .tl-handle-dot,.tl-handle.dragging .tl-handle-dot { transform:scale(1.35); }
.tl-remove { position:absolute; bottom:calc(100% + 4px); left:50%; transform:translateX(-50%); width:20px; height:20px; background:rgba(200,40,40,.9); border:none; border-radius:50%; color:#fff; font-size:13px; font-weight:700; display:flex; align-items:center; justify-content:center; opacity:0; transition:opacity .15s; cursor:pointer; pointer-events:all; padding:0; line-height:1; z-index:30; white-space:nowrap; }
.tl-handle:hover .tl-remove { opacity:1; }
.tl-hint { font-size:11px; color:var(--muted); text-align:center; margin-top:7px; letter-spacing:.3px; }
/* Tick ruler */
.tl-ruler { position:relative; height:12px; margin-top:3px; overflow:visible; }
.tl-tick { position:absolute; transform:translateX(-50%); width:1px; background:var(--border); }
.tl-tick-hour { height:9px; background:rgba(80,110,180,0.8); }
.tl-tick-half { height:5px; background:rgba(50,70,120,0.6); }
/* Axis hour labels */
.tl-axis { position:relative; height:20px; margin-top:1px; }
.tl-axis-mark { position:absolute; transform:translateX(-50%); display:flex; flex-direction:column; align-items:center; pointer-events:none; }
.tl-axis-static { font-family:'Share Tech Mono',monospace; font-size:12px; color:#6a80b0; white-space:nowrap; letter-spacing:0.3px; }
/* Handle hour labels — draggable, colored + bold */
.tl-axis-hour { font-family:'Share Tech Mono',monospace; font-size:13px; font-weight:700; color:var(--white); white-space:nowrap; text-shadow:0 0 6px rgba(0,0,0,0.9); }
/* Brightness sliders below timeline */
.sched-bright-row { display:flex; align-items:center; gap:10px; padding:4px 0; }
.sched-bright-label { font-size:13px; width:152px; flex-shrink:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.sched-bright-row input[type=range] { flex:1; height:3px; accent-color:var(--amber); cursor:pointer; margin:0; }
.sched-pct { font-family:'Share Tech Mono',monospace; font-size:13px; color:var(--amber); min-width:36px; text-align:right; }
.sched-remove { background:rgba(220,50,50,.85); border:none; border-radius:50%; color:#fff; width:18px; height:18px; font-size:12px; font-weight:700; line-height:1; display:flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; padding:0; opacity:0.6; transition:opacity .15s; }
.sched-remove:hover { opacity:1; }
/* Preview box states */
#livePreviewBox img { width:100%; height:100%; object-fit:cover; display:block; }
#livePreviewBox svg { opacity:.7; }

/* ── Toast ── */
#toast { position:fixed; bottom:calc(env(safe-area-inset-bottom,0px) + 20px); left:50%; transform:translateX(-50%) translateY(80px); background:var(--panel); border:1px solid var(--border); color:var(--text); padding:10px 20px; border-radius:6px; font-family:'Share Tech Mono',monospace; font-size:13px; letter-spacing:1px; white-space:nowrap; transition:transform .25s cubic-bezier(.34,1.56,.64,1); z-index:999; pointer-events:none; }
#toast.show { transform:translateX(-50%) translateY(0); }
#toast.ok  { border-color:rgba(0,255,136,.4); color:var(--green); text-shadow:0 0 8px var(--green); }
#toast.err { border-color:rgba(255,34,68,.4); color:#ff6688; }
</style>
</head>
<body>
<div class="page-wrap">

<div class="header">
  <div>
    <div class="header-title">{{ title }}</div>
    <div class="header-sub">Raspberry Pi 4 &nbsp;·&nbsp; 64×64 RGB</div>
  </div>
  <div class="live-badge"><div class="live-dot"></div>LIVE</div>
</div>

<div class="dash-grid">

<!-- ── Row 1: Now on Display  |  Brightness ── -->

<!-- Now on Display -->
<div class="section">
  <div class="section-title">// Now on Display</div>
  <div class="card">
    <div style="display:flex;align-items:center;gap:20px;margin-bottom:20px">
      <div id="livePreviewBox" style="flex-shrink:0;width:110px;height:110px;border-radius:10px;overflow:hidden;background:#0a0f1a;border:1px solid var(--border);display:flex;align-items:center;justify-content:center;transition:background .3s"></div>
      <div style="min-width:0;flex:1">
        <div style="font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:2.5px;color:var(--muted);margin-bottom:10px;text-transform:uppercase">Now on Display</div>
        <div style="font-family:'Share Tech Mono',monospace;font-size:22px;color:var(--white);letter-spacing:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:6px" id="liveSlideLabel">—</div>
        <div style="font-size:13px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" id="liveSlideDetail">Loading…</div>
      </div>
    </div>
    <hr>
    <div class="section-title" style="margin-bottom:10px">// Slideshow Lock</div>
    <div class="slide-grid" style="grid-template-columns:1fr 1fr 1fr">
      <div class="slide-item">
        <button class="slide-btn" id="slide-clock" onclick="setSlide('clock')">
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="11" cy="11" r="9"/><path d="M11 6v5l3 3"/></svg>
          Clock
        </button>
        <button class="slide-power" id="power-clock" onclick="toggleSlideOff('clock')" title="Disable slide">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
        </button>
      </div>
      <div class="slide-item">
        <button class="slide-btn" id="slide-weather" onclick="setSlide('weather')">
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M16 9.5a4 4 0 00-7.8-1.2A3.5 3.5 0 105 15.5h11a3 3 0 000-6z"/><circle cx="17" cy="5" r="2" fill="currentColor" stroke="none" opacity=".8"/><path d="M17 1.5v1M17 7v1M13.5 3l.7.7M20.5 3l-.7.7M12 5h1M21 5h1" stroke-width="1.2"/></svg>
          Weather
        </button>
        <button class="slide-power" id="power-weather" onclick="toggleSlideOff('weather')" title="Disable slide">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
        </button>
      </div>
      <div class="slide-item">
        <button class="slide-btn" id="slide-sun" onclick="setSlide('sun')">
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 17h18"/><path d="M11 13a4 4 0 100-8 4 4 0 000 8z"/><path d="M11 2v1.5M11 18.5V17M3.5 4.5l1 1M17.5 4.5l-1 1M1.5 11H3M19 11h1.5"/></svg>
          Sunrise
        </button>
        <button class="slide-power" id="power-sun" onclick="toggleSlideOff('sun')" title="Disable slide">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
        </button>
      </div>
      <div class="slide-item">
        <button class="slide-btn" id="slide-photos" onclick="setSlide('photos')">
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="18" height="14" rx="2"/><circle cx="8" cy="10" r="2"/><path d="M2 15.5l4.5-4 3.5 3.5 3-3 5 5"/><path d="M6 5V3.5A1.5 1.5 0 017.5 2h7A1.5 1.5 0 0116 3.5V5"/></svg>
          Photos
        </button>
        <button class="slide-power" id="power-photos" onclick="toggleSlideOff('photos')" title="Disable slide">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
        </button>
      </div>
      <div class="slide-item">
        <button class="slide-btn" id="slide-flights" onclick="setSlide('flights')">
          <svg width="22" height="22" viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z" fill="currentColor" stroke="none"/></svg>
          Flights
        </button>
        <button class="slide-power" id="power-flights" onclick="toggleSlideOff('flights')" title="Disable slide">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
        </button>
      </div>
      <div class="slide-item">
        <button class="slide-btn" id="slide-iss" onclick="setSlide('iss')">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="10" width="6" height="4" rx="1"/><line x1="3" y1="12" x2="9" y2="12"/><line x1="15" y1="12" x2="21" y2="12"/><rect x="1" y="10" width="2" height="4"/><rect x="21" y="10" width="2" height="4"/></svg>
          ISS
        </button>
        <button class="slide-power" id="power-iss" onclick="toggleSlideOff('iss')" title="Disable slide">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
        </button>
      </div>
    </div>
    <button class="slide-btn" id="slide-auto" onclick="setSlideAuto()" style="flex-direction:row;padding:13px 16px;gap:10px">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
      Auto — Resume Slideshow
    </button>
  </div>
</div>

<!-- Auto Schedule -->
<div class="section">
  <div class="section-title">// Auto Schedule</div>
  <div class="card">
    <div id="schedTimeline" style="margin-bottom:14px"></div>
    <div id="schedSliders"></div>
  </div>
</div>

<!-- Clock Style -->
<div class="section">
  <div class="section-title">// Clock Style</div>
  <div class="card">
    <div class="row" style="gap:8px;margin-bottom:14px">
      <button class="btn-ghost" id="clkModeSingle" onclick="setClockMode('single')" style="flex:1">Single — Eastern</button>
      <button class="btn-ghost" id="clkModeQuad" onclick="setClockMode('quad')" style="flex:1">4 Time Zones</button>
    </div>
    <div id="clkZones" style="display:none">
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px;letter-spacing:1px">PICK FOUR ZONES (top-left, top-right, bottom-left, bottom-right)</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        <select class="clk-tz" id="clkTz0"></select>
        <select class="clk-tz" id="clkTz1"></select>
        <select class="clk-tz" id="clkTz2"></select>
        <select class="clk-tz" id="clkTz3"></select>
      </div>
      <button class="btn-primary" onclick="saveClockZones()" style="margin-top:12px;width:100%">Save Time Zones</button>
    </div>
  </div>
</div>

<!-- ── Row 2: System Status  |  Live Radar ── -->

<!-- System Status -->
<div class="section">
  <div class="section-title">// System Status</div>
  <div class="card">
    <!-- Brightness override -->
    <div style="margin-bottom:16px">
      <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:4px">
        <div class="bright-val" id="brightVal" style="font-size:clamp(28px,4vw,38px);text-align:left">–</div>
        <div class="bright-auto-tag" id="brightTag" style="text-align:left;font-size:11px">LOADING</div>
      </div>
      <input type="range" id="brightSlider" min="0" max="100" value="100" style="margin:6px 0 10px">
      <div class="row">
        <button class="btn-ghost" onclick="setBrightness(0)">Off</button>
        <button class="btn-ghost" onclick="setBrightness(35)">35%</button>
        <button class="btn-ghost" onclick="setBrightness(100)">Full</button>
        <button class="btn-ghost" id="btnBrightAuto" onclick="setBrightnessAuto()">Auto</button>
      </div>
    </div>
    <hr>
    <div class="status-inner">
      <div>
        <div class="status-row">
          <span class="status-label">Dashboard</span>
          <span class="status-val" id="stDash">…</span>
        </div>
        <div class="status-row">
          <span class="status-label">CPU Temp</span>
          <span class="status-val" id="stTemp">…</span>
        </div>
        <div class="status-row">
          <span class="status-label">Art Cache</span>
          <span class="status-val" id="stCache">…</span>
        </div>
        <div class="status-row">
          <span class="status-label">Forced Photo</span>
          <span class="status-val" id="stForced">none</span>
        </div>
        <div class="status-row">
          <span class="status-label">Slide Lock</span>
          <span class="status-val" id="stSlide">auto</span>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <button class="btn-primary" onclick="restartService('display')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;margin-right:5px"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          Restart Display
        </button>
        <button class="btn-primary" onclick="restartService('web')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;margin-right:5px"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          Restart Web
        </button>
        <button class="btn-danger" onclick="clearArtCache()">Clear Art Cache</button>
      </div>
    </div>
  </div>
</div>

<!-- Live Radar -->
<div class="section">
  <div class="section-title">// Live Radar</div>
  <div class="card" style="padding:0;overflow:hidden;border-radius:8px">
    <div id="radarMap" style="height:280px;width:100%"></div>
    <div id="radarList" style="padding:12px 16px 10px"></div>
  </div>
</div>

<!-- ── Row 3: Manual Flight Track  |  ISS + API Monitors ── -->

<!-- Manual Flight Track -->
<div class="section">
  <div class="section-title">// Manual Flight Track</div>
  <div class="card" id="flightCard"></div>
</div>

<!-- ISS Tracker + API Monitors stacked -->
<div class="dash-stack">
  <div class="section">
    <div class="section-title">// ISS Tracker</div>
    <div class="card" id="issCard"><div class="radar-empty">Loading…</div></div>
  </div>
  <div class="section">
    <div class="section-title">// API Monitors</div>
    <div class="monitor-grid">
      <div class="monitor-card">
        <div class="monitor-label"><div class="monitor-dot" id="dotWeather"></div>Weather</div>
        <div style="display:flex;align-items:baseline;gap:8px">
          <div class="monitor-val" id="monWeatherTemp">–</div>
          <div style="font-size:12px;color:var(--muted)" id="monWeatherFeels"></div>
        </div>
        <div class="monitor-sub" id="monWeatherDesc">–</div>
        <div class="monitor-stats" id="monWeatherStats" style="display:none">
          <span id="monWeatherHumid"></span>
          <span id="monWeatherWind"></span>
          <span id="monWeatherUV"></span>
          <span id="monWeatherPrecip"></span>
        </div>
        <div class="monitor-tag" id="monWeatherAge">–</div>
      </div>
      <div class="monitor-card">
        <div class="monitor-label"><div class="monitor-dot" id="dotMusic"></div>Music</div>
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:6px">
          <img id="monMusicArt" src="" alt="" style="width:48px;height:48px;border-radius:4px;object-fit:cover;background:var(--surface);display:none;flex-shrink:0;image-rendering:auto">
          <div style="min-width:0">
            <div class="monitor-val" id="monMusicTrack" style="font-size:15px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">–</div>
            <div class="monitor-sub" id="monMusicArtist" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">–</div>
          </div>
        </div>
        <div class="monitor-tag" id="monMusicSource">–</div>
      </div>
    </div>
  </div>
</div>

<!-- ── Full width: Photos ── -->
<div class="section dash-full">
  <div class="section-title">// Photos</div>
  <div class="card">
    <div class="photos-inner">
      <div>
        <form id="uploadForm" onsubmit="submitUpload(event)">
          <div class="upload-zone" id="uploadZone">
            <input type="file" name="photos" multiple accept="image/*" id="fileInput">
            <div class="upload-icon"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg></div>
            <div id="uploadLabel">Tap to add photos</div>
            <div class="upload-hint">JPG · PNG · HEIC</div>
          </div>
          <button class="btn-success" type="submit">Upload to Dashboard</button>
        </form>
        <div id="forcedBanner" class="forced-banner">
          <div class="forced-banner-dot" id="forcedBannerDot"></div>
          <span class="forced-banner-label" id="forcedBannerLabel"></span>
          <button class="forced-banner-stop" onclick="clearForcedPhoto()">Stop</button>
        </div>
        <div class="photo-count-bar">
          <span id="photoCountLabel">Loading…</span>
          <button class="btn-danger btn-icon" id="deleteAllBtn" onclick="deleteAllPhotos()" style="display:none">Delete All</button>
        </div>
      </div>
      <div>
        <div class="photo-grid" id="photoGrid"></div>
      </div>
    </div>
  </div>
</div>


</div><!-- /dash-grid -->
</div><!-- /page-wrap -->

<!-- Show Photo Modal -->
<div class="photo-overlay" id="photoOverlay" onclick="if(event.target===this)closeShowModal()">
  <div class="photo-modal">
    <div class="photo-modal-title">Show on LED</div>
    <div class="photo-modal-name" id="modalFilename"></div>
    <div class="photo-modal-actions">
      <button class="btn-primary" onclick="confirmShowPhoto('freeze')">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6v6H9z"/></svg>
        Freeze on display
      </button>
      <button class="btn-success" onclick="confirmShowPhoto('slideshow')">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 9A6 6 0 113 9"/><path d="M15 4.5V9h-4.5"/></svg>
        Add to slideshow
      </button>
    </div>
    <button class="photo-modal-cancel" onclick="closeShowModal()">Cancel</button>
  </div>
</div>

<div class="edit-popup" id="editPopup">
  <div class="edit-popup-title">Edit Photo</div>
  <img id="epPreview" src="" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;margin-bottom:10px;image-rendering:pixelated;background:#111;">
  <div class="edit-row"><label>Zoom</label><input type="range" id="epZoom" min="0.5" max="3.0" step="0.05" oninput="epVal('epZoomVal',this.value,'x');epRefresh()"><span class="edit-val" id="epZoomVal">1.0x</span></div>
  <div class="edit-row"><label>X Pan</label><input type="range" id="epX" min="-32" max="32" step="1" oninput="epVal('epXVal',this.value,'px');epRefresh()"><span class="edit-val" id="epXVal">0px</span></div>
  <div class="edit-row"><label>Y Pan</label><input type="range" id="epY" min="-32" max="32" step="1" oninput="epVal('epYVal',this.value,'px');epRefresh()"><span class="edit-val" id="epYVal">0px</span></div>
  <div class="edit-row"><label>Brightness</label><input type="range" id="epBright" min="0.2" max="2.0" step="0.05" oninput="epVal('epBrightVal',this.value,'x');epRefresh()"><span class="edit-val" id="epBrightVal">1.0x</span></div>
  <div class="edit-popup-actions">
    <button class="btn-primary" onclick="saveEditPopup()">Save</button>
    <button class="btn-cancel" onclick="closeEditPopup()">Cancel</button>
  </div>
</div>

<div id="toast"></div>

<script>
// Toast
const toast = document.getElementById('toast');
let toastTimer;
function showToast(msg, ok=true) {
  toast.textContent = msg;
  toast.className = 'show ' + (ok ? 'ok' : 'err');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.className = '', 2800);
}

// Brightness
const slider = document.getElementById('brightSlider');
const brightV = document.getElementById('brightVal');
const brightT = document.getElementById('brightTag');
const btnBA   = document.getElementById('btnBrightAuto');
function updateBrightnessUI(pct, isAuto) {
  slider.value = pct;
  brightV.textContent = pct + '%';
  brightT.textContent = isAuto ? 'AUTO – FOLLOWS SCHEDULE' : 'MANUAL OVERRIDE';
  brightT.style.color = isAuto ? '' : 'var(--amber)';
  btnBA.classList.toggle('btn-active', isAuto);
}
slider.addEventListener('input', () => {
  brightV.textContent = slider.value + '%';
  brightT.textContent = 'MANUAL OVERRIDE';
  brightT.style.color = 'var(--amber)';
  btnBA.classList.remove('btn-active');
});
slider.addEventListener('change', () => setBrightness(parseInt(slider.value)));
async function setBrightness(pct) {
  slider.value = pct; brightV.textContent = pct + '%';
  brightT.textContent = 'MANUAL OVERRIDE'; brightT.style.color = 'var(--amber)';
  btnBA.classList.remove('btn-active');
  await fetch('/api/brightness', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:pct/100})});
  showToast('Brightness set to ' + pct + '%');
}
async function setBrightnessAuto() {
  await fetch('/api/brightness', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({auto:true})});
  showToast('Brightness set to Auto');
  pollOverride();
}

// Slide lock + disabled slides
const SLIDES = ['clock','weather','sun','photos','flights','iss'];
let currentSlide = null;
let disabledSlides = [];

function updateSlideUI(lock) {
  currentSlide = lock;
  SLIDES.forEach(s => {
    const b = document.getElementById('slide-' + s);
    const p = document.getElementById('power-' + s);
    const off = disabledSlides.includes(s);
    if (b) { b.classList.toggle('btn-active', s === lock); b.classList.toggle('slide-off', off); }
    if (p) p.classList.toggle('slide-off', off);
  });
  const autoBtn = document.getElementById('slide-auto');
  if (autoBtn) autoBtn.classList.toggle('btn-active', !lock);
  const stSlide = document.getElementById('stSlide');
  if (stSlide) { stSlide.textContent = lock || 'auto'; stSlide.className = 'status-val ' + (lock ? 'status-warn' : 'status-ok'); }
}

function updateDisabledUI(list) {
  disabledSlides = list || [];
  updateSlideUI(currentSlide);
}

async function toggleSlideOff(name) {
  const nowOff = disabledSlides.includes(name);
  disabledSlides = nowOff ? disabledSlides.filter(s => s !== name) : [...disabledSlides, name];
  updateSlideUI(currentSlide);
  await fetch('/api/slide_disabled', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({disabled:disabledSlides})});
  showToast(name.charAt(0).toUpperCase() + name.slice(1) + (nowOff ? ' enabled' : ' disabled'));
}

async function setSlide(name) {
  await fetch('/api/slide', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({lock:name})});
  updateSlideUI(name);
  showToast('Locked to ' + name.charAt(0).toUpperCase() + name.slice(1));
}
async function setSlideAuto() {
  await fetch('/api/slide', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({auto:true})});
  updateSlideUI(null);
  updateForcedPhotoUI(null, null);
  showToast('Slideshow resumed');
}

// Override poll
// Slide icon SVGs keyed by slot name
const SLIDE_ICONS = {
  clock:   `<svg width="36" height="36" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="11" cy="11" r="9"/><path d="M11 6v5l3 3"/></svg>`,
  weather: `<svg width="36" height="36" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M16 9.5a4 4 0 00-7.8-1.2A3.5 3.5 0 105 15.5h11a3 3 0 000-6z"/></svg>`,
  sun:     `<svg width="36" height="36" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 17h18"/><path d="M11 13a4 4 0 100-8 4 4 0 000 8z"/><path d="M11 2v1.5M11 18.5V17M3.5 4.5l1 1M17.5 4.5l-1 1M1.5 11H3M19 11h1.5"/></svg>`,
  photos:  `<svg width="36" height="36" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="18" height="14" rx="2"/><circle cx="8" cy="10" r="2"/><path d="M2 15.5l4.5-4 3.5 3.5 3-3 5 5"/></svg>`,
  flights: `<svg width="36" height="36" viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z" fill="currentColor" stroke="none"/></svg>`,
  iss:     `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="10" width="6" height="4" rx="1"/><line x1="3" y1="12" x2="9" y2="12"/><line x1="15" y1="12" x2="21" y2="12"/><rect x="1" y="10" width="2" height="4"/><rect x="21" y="10" width="2" height="4"/></svg>`,
  off:     `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>`,
};

let _lastOverride = {};
let _lastLiveData = {};

function updateNowOnDisplay() {
  const ov = _lastOverride, ld = _lastLiveData;
  const box = document.getElementById('livePreviewBox');
  const label = document.getElementById('liveSlideLabel');
  const detail = document.getElementById('liveSlideDetail');
  if (!box) return;

  const lock = ov.slide_lock || 'auto';
  const fp = ov.forced_photo;
  const effPct = ov.effective_pct !== undefined ? ov.effective_pct : 100;
  const music = ld.music;
  const isOff = effPct <= 0;

  if (isOff) {
    box.style.background = '#010306';
    box.style.color = 'var(--muted)';
    box.innerHTML = SLIDE_ICONS.off;
    label.textContent = 'OFF';
    detail.textContent = 'Display is off';
  } else if (music && music.track) {
    // Music / album art showing on screen
    const t = Math.round(Date.now()/2000);
    box.style.background = '#0a0f1a';
    box.innerHTML = `<img src="/api/current_art?t=${t}" style="width:80px;height:80px;object-fit:cover;border-radius:6px;image-rendering:auto" onerror="this.style.display='none'">`;
    label.textContent = music.track.length > 22 ? music.track.slice(0,20)+'…' : music.track;
    detail.textContent = music.artist || '';
  } else if (lock === 'photos' && fp) {
    // Frozen photo
    fetch('/api/display_preview').then(r => r.ok ? r.blob() : null).then(b => {
      if (!b) return;
      const url = URL.createObjectURL(b);
      box.style.background = '#000';
      box.innerHTML = `<img src="${url}" style="width:80px;height:80px;object-fit:cover;border-radius:6px;image-rendering:pixelated">`;
    });
    label.textContent = 'PHOTO';
    detail.textContent = fp;
  } else if (lock && lock !== 'auto' && SLIDE_ICONS[lock]) {
    box.style.background = 'rgba(34,136,255,.08)';
    box.style.color = 'var(--cyan)';
    box.innerHTML = SLIDE_ICONS[lock];
    label.textContent = lock.toUpperCase();
    detail.textContent = 'Manual lock';
  } else {
    // Auto cycling — show generic cycling indicator
    box.style.background = 'rgba(0,255,136,.05)';
    box.style.color = 'var(--green)';
    box.innerHTML = `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>`;
    label.textContent = 'AUTO';
    detail.textContent = 'Cycling slides';
  }
}

// ── Schedule timeline (flexible segments) ────────────────────────────────────
// Data: _segs = [{bright:int, end?:float}, ...]
//   • Covers 0 → 24h; last entry has no 'end' (goes to midnight)
//   • Handles sit at segs[i].end between segment i and i+1
//   • Click the bar to ADD a handle; hover a handle and click × to REMOVE it
let _segs = [];
const _HANDLE_COLORS = ['#5a8aff','#44ffaa','#ffb055','#ff88cc','#00e5ff','#ff5566'];

function _segEnd(i)    { return i < _segs.length - 1 ? _segs[i].end : 24; }
function _segStart(i)  { return i === 0 ? 0 : _segs[i-1].end; }
function _segWidth(i)  { return (_segEnd(i) - _segStart(i)) / 24 * 100; }
function _hColor(i)    { return _HANDLE_COLORS[i % _HANDLE_COLORS.length]; }
function _segBg(b)     { return `rgba(28,75,160,${(0.12 + b/100*0.85).toFixed(2)})`; }
function _segIcon(b)   { return b<=5?'⬛':b<=20?'🌙':b<=50?'🌅':b<=80?'⛅':'☀️'; }
function _fmtHour(h) {
  const w=Math.floor(h), half=(h%1)!==0, h12=w%12||12, ap=w<12?'am':'pm';
  return half ? `${h12}:30${ap}` : `${h12}${ap}`;
}

function renderScheduleEditor(data) {
  if (data && Array.isArray(data.segments) && data.segments.length) {
    _segs = data.segments.map(s => ({...s}));
  } else if (data && data.night_end != null) {
    // Convert legacy 4-period format
    _segs = [
      {end:+data.night_end,   bright:+data.night_bright   ||0},
      {end:+data.morning_end, bright:+data.morning_bright ||35},
      {end:+data.day_end,     bright:+data.day_bright     ||100},
      {end:+data.evening_end, bright:+data.evening_bright ||35},
      {bright:+data.night_bright||0},
    ].filter((s,i,a) => i===a.length-1 || s.end>(i===0?0:a[i-1].end)+0.01);
  } else {
    _segs = [{bright:100}];
  }
  _renderTimeline();
  _renderSegsSliders();
}

function _removeHandle(i) {
  // Left segment absorbs right; keeps left's brightness
  const ns = _segs.map(s=>({...s}));
  if (i + 1 < ns.length - 1) { ns[i].end = ns[i+1].end; }
  else                        { delete ns[i].end; }
  ns.splice(i+1, 1);
  _segs = ns;
  _renderTimeline();
  _renderSegsSliders();
  _schedAutoSave();
}

function _addHandle(clientX) {
  const bar = document.getElementById('tlBarWrap');
  if (!bar) return;
  const rect = bar.getBoundingClientRect();
  const hour = Math.round((clientX - rect.left) / rect.width * 48) / 2;
  for (let i = 0; i < _segs.length; i++) {
    const s = _segStart(i), e = _segEnd(i);
    if (hour > s + 0.4 && hour < e - 0.4) {
      const ns = _segs.map(x=>({...x}));
      const right = {bright: ns[i].bright};
      if (ns[i].end !== undefined) right.end = ns[i].end;
      ns[i].end = hour;
      ns.splice(i+1, 0, right);
      _segs = ns;
      _renderTimeline();
      _renderSegsSliders();
      _schedAutoSave();
      return;
    }
  }
}

function _renderTimeline() {
  const wrap = document.getElementById('schedTimeline');
  if (!wrap) return;
  const numH = _segs.length - 1;

  const labels = _segs.map((seg,i) => {
    const w=_segWidth(i).toFixed(2), vis=w>8?'1':'0';
    const timeStr = `${_fmtHour(_segStart(i))} – ${_fmtHour(_segEnd(i))}`;
    return `<div class="tl-label-cell" id="tlCell${i}" style="width:${w}%">
      <span class="tl-label-time" id="tlTime${i}" style="opacity:${vis};transition:opacity .15s">${timeStr}</span>
      <span class="tl-label-pct" id="tlPct${i}" style="opacity:${vis};transition:opacity .15s">${seg.bright}%</span>
    </div>`;
  }).join('');

  const bar = _segs.map((seg,i) =>
    `<div class="tl-seg" id="tlSeg${i}" style="width:${_segWidth(i).toFixed(2)}%;background:${_segBg(seg.bright)}"></div>`
  ).join('');

  const handles = Array.from({length:numH},(_,i) => {
    const pct=(_segs[i].end/24*100).toFixed(2), col=_hColor(i);
    return `<div class="tl-handle" id="tlH${i}" style="left:${pct}%">
      <button class="tl-remove" id="tlRemove${i}" title="Remove boundary">×</button>
      <div class="tl-handle-dot" style="background:${col};box-shadow:0 0 7px ${col}88"></div>
    </div>`;
  }).join('');

  let ruler='';
  for(let i=0;i<=48;i++){const p=(i*.5/24*100).toFixed(2),isH=i%2===0;ruler+=`<div class="tl-tick ${isH?'tl-tick-hour':'tl-tick-half'}" style="left:${p}%;top:${isH?3:6}px"></div>`;}

  let axis='';
  for(let h=3;h<=21;h+=3) axis+=`<div class="tl-axis-mark" style="left:${(h/24*100).toFixed(2)}%"><span class="tl-axis-static">${_fmtHour(h)}</span></div>`;

  const hint = numH===0
    ? '<div class="tl-hint">☝ Click the bar to split it and add a boundary</div>'
    : '<div class="tl-hint">Drag to move &nbsp;·&nbsp; Hover handle to remove &nbsp;·&nbsp; Click bar to add</div>';

  wrap.innerHTML = `<div class="tl-wrap">
    <div class="tl-labels" id="tlLabels">${labels}</div>
    <div class="tl-bar-wrap" id="tlBarWrap">
      <div class="tl-bar">${bar}</div>
      <div class="tl-handles">${handles}</div>
    </div>
    <div class="tl-ruler">${ruler}</div>
    <div class="tl-axis">${axis}</div>
    ${hint}
  </div>`;

  for(let i=0;i<numH;i++){
    const el=document.getElementById('tlH'+i); if(el) _attachTlDrag(el,i);
    const rb=document.getElementById('tlRemove'+i);
    if(rb){
      rb.addEventListener('pointerdown', e=>e.stopPropagation());
      rb.addEventListener('click',       e=>{ e.stopPropagation(); _removeHandle(i); });
    }
  }
  const bw=document.getElementById('tlBarWrap');
  if(bw) bw.addEventListener('click',e=>{ if(!e.target.closest('.tl-handle')) _addHandle(e.clientX); });
}

function _tlUpdate() {
  _segs.forEach((seg,i) => {
    const w=_segWidth(i).toFixed(2), vis=w>8?'1':'0';
    const se=document.getElementById('tlSeg'+i), ce=document.getElementById('tlCell'+i);
    const pe=document.getElementById('tlPct'+i), te=document.getElementById('tlTime'+i);
    if(se){se.style.width=w+'%';se.style.background=_segBg(seg.bright);}
    if(ce) ce.style.width=w+'%';
    if(pe){pe.textContent=seg.bright+'%';pe.style.opacity=vis;}
    if(te){te.style.opacity=vis; te.textContent=`${_fmtHour(_segStart(i))} – ${_fmtHour(_segEnd(i))}`; }
  });
  for(let i=0;i<_segs.length-1;i++){
    const p=(_segs[i].end/24*100).toFixed(2);
    const he=document.getElementById('tlH'+i);
    if(he) he.style.left=p+'%';
  }
}

function _attachTlDrag(el, i) {
  let active=false;
  el.addEventListener('pointerdown', e => {
    if(e.target.closest('.tl-remove')) return;
    e.preventDefault(); active=true; el.setPointerCapture(e.pointerId); el.classList.add('dragging');
  });
  el.addEventListener('pointermove', e => {
    if(!active) return;
    const bar=document.getElementById('tlBarWrap'); if(!bar) return;
    const rect=bar.getBoundingClientRect();
    let hour=Math.round(Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width))*96)/4;
    const lo=i===0?0.5:_segs[i-1].end+0.5;
    const hi=i>=_segs.length-2?23.5:_segs[i+1].end-0.5;
    _segs[i].end=Math.max(lo,Math.min(hi,hour));
    _tlUpdate();
  });
  el.addEventListener('pointerup',     ()=>{active=false;el.classList.remove('dragging');_schedAutoSave();});
  el.addEventListener('pointercancel', ()=>{active=false;el.classList.remove('dragging');_schedAutoSave();});
}

function _renderSegsSliders() {
  const c=document.getElementById('schedSliders'); if(!c) return;
  const n=_segs.length;
  // Merge first and last rows when they share the same brightness (overnight wrap)
  const wrapMerge = n > 1 && _segs[0].bright === _segs[n-1].bright;
  const rows=[];
  const start = wrapMerge ? 1 : 0;
  for(let i=start; i<n; i++){
    const seg=_segs[i];
    let label, col, removeBtn, pctId, handler;
    if(wrapMerge && i===n-1){
      // Merged overnight row: last-segment-start → first-segment-end
      label = `${_fmtHour(_segStart(n-1))} – ${_fmtHour(_segEnd(0))}`;
      col = _hColor(n-1);
      pctId = 'svb_merged';
      handler = `_onBrightChangeMerged(${n-1},0,this.value)`;
      removeBtn = `<span style="width:18px;flex-shrink:0"></span>`;
    } else {
      label = `${_fmtHour(_segStart(i))} – ${_fmtHour(_segEnd(i))}`;
      col = _hColor(i);
      pctId = `svb_${i}`;
      handler = `_onBrightChange(${i},this.value)`;
      removeBtn = i < n-1
        ? `<button class="sched-remove" id="sbRemove${i}" title="Remove boundary">×</button>`
        : `<span style="width:18px;flex-shrink:0"></span>`;
    }
    rows.push(`<div class="sched-bright-row">
      <span class="sched-bright-label" style="color:${col}">${label}</span>
      <input type="range" min="0" max="100" step="5" value="${seg.bright}" oninput="${handler}">
      <span class="sched-pct" id="${pctId}">${seg.bright}%</span>
      ${removeBtn}
    </div>`);
  }
  c.innerHTML=rows.join('');
  for(let i=start; i<n-1; i++){
    const rb=document.getElementById('sbRemove'+i);
    if(rb) rb.addEventListener('click', ()=>_removeHandle(i));
  }
}

function _onBrightChange(i, val) {
  _segs[i].bright=parseInt(val);
  const el=document.getElementById('svb_'+i); if(el) el.textContent=val+'%';
  _tlUpdate(); _schedAutoSave();
}

function _onBrightChangeMerged(a, b, val) {
  _segs[a].bright=parseInt(val); _segs[b].bright=parseInt(val);
  const el=document.getElementById('svb_merged'); if(el) el.textContent=val+'%';
  _tlUpdate(); _schedAutoSave();
}

let _schedSaveTimer=null;
function _schedAutoSave() { clearTimeout(_schedSaveTimer); _schedSaveTimer=setTimeout(_schedCommit,700); }

async function _schedCommit() {
  if(!_segs||!_segs.length) return;
  try {
    const r=await fetch('/api/bright_schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({segments:_segs.map(s=>({...s}))})});
    if(r.ok) showToast('Schedule saved');
  } catch(e){showToast('Save failed');}
}

function saveSchedule() { clearTimeout(_schedSaveTimer); _schedCommit(); }

async function loadSchedule() {
  let s;
  try { const r=await fetch('/api/bright_schedule'); if(r.ok) s=await r.json(); } catch(e){}
  renderScheduleEditor(s || {segments:[{bright:100}]});
}

async function pollOverride() {
  try {
    const r = await fetch('/api/override');
    const d = await r.json();
    _lastOverride = d;
    const b = d.brightness, isAuto = b === null || b === undefined;
    updateBrightnessUI(d.effective_pct !== undefined ? d.effective_pct : (isAuto ? 100 : Math.round(b*100)), isAuto);
    updateDisabledUI(d.disabled_slides || []);
    updateSlideUI(d.slide_lock || null);
    const fp = d.forced_photo || null;
    const fm = fp ? (d.slide_lock === 'photos' ? 'freeze' : 'slideshow') : null;
    updateForcedPhotoUI(fp, fm);
    updateNowOnDisplay();
  } catch(e) {}
}

// Manual flight tracking
const fc = document.getElementById('flightCard');
let lastStatus = null, lastCallsign = null;
function flightIdle() {
  fc.innerHTML = `
    <div class="flight-card">
      <div class="flight-badge badge-idle">● IDLE</div>
      <div style="color:var(--muted);font-size:13px">No flight being tracked</div>
    </div>
    <form onsubmit="submitTrack(event)">
      <div class="input-label">Flight Callsign</div>
      <input type="text" id="csInput" placeholder="DAL123" autocomplete="off" autocorrect="off" autocapitalize="characters" spellcheck="false">
      <button class="btn-primary" type="submit">Track Flight</button>
    </form>`;
}
function flightSearching(cs) {
  fc.innerHTML = `
    <div class="flight-card state-searching">
      <div class="flight-badge badge-searching">◉ SCANNING<span class="dot-anim"></span></div>
      <div class="callsign-big">${cs}</div>
      <div class="flight-sub">Scanning ADS-B network globally…</div>
    </div>
    <button class="btn-danger" onclick="stopTrack()">Stop Tracking</button>`;
}
function flightTracking(cs) {
  fc.innerHTML = `
    <div class="flight-card state-tracking">
      <div class="flight-badge badge-tracking">● LIVE</div>
      <div class="callsign-big">${cs}</div>
      <div class="flight-sub">Showing on display</div>
    </div>
    <button class="btn-danger" onclick="stopTrack()">Stop Tracking</button>`;
}
function flightNotFound(cs) {
  fc.innerHTML = `
    <div class="flight-card state-notfound">
      <div class="flight-badge badge-notfound">✕ NOT FOUND</div>
      <div class="callsign-big">${cs || '—'}</div>
      <div class="flight-sub">Not found in ADS-B network</div>
    </div>
    <form onsubmit="submitTrack(event)" style="margin-top:12px">
      <div class="input-label">Try Another Callsign</div>
      <input type="text" id="csInput" placeholder="DAL123" autocomplete="off" autocorrect="off" autocapitalize="characters" spellcheck="false">
      <button class="btn-primary" type="submit">Track Flight</button>
    </form>`;
}
async function submitTrack(e) {
  e.preventDefault();
  const cs = (document.getElementById('csInput')?.value || '').trim().toUpperCase();
  if (!cs) return;
  await fetch('/track', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'callsign='+encodeURIComponent(cs)});
  showToast('Searching for ' + cs + '…');
  pollFlight();
}
async function stopTrack() {
  await fetch('/track/clear', {method:'POST'});
  showToast('Flight tracking stopped');
  pollFlight();
}
async function pollFlight() {
  try {
    const d = await (await fetch('/api/track')).json();
    const cs = d.callsign, status = d.status || (cs ? 'tracking' : 'idle');
    if (status === lastStatus && cs === lastCallsign) return;
    lastStatus = status; lastCallsign = cs;
    if (status === 'tracking')       flightTracking(cs);
    else if (status === 'searching') flightSearching(cs || lastCallsign);
    else if (status === 'not_found') flightNotFound(d.last_searched);
    else flightIdle();
  } catch(e) {}
}

// Live data helpers
const _WMO = {0:'Clear sky',1:'Mainly clear',2:'Partly cloudy',3:'Overcast',45:'Foggy',48:'Icy fog',51:'Light drizzle',53:'Drizzle',55:'Heavy drizzle',61:'Light rain',63:'Rain',65:'Heavy rain',71:'Light snow',73:'Snow',75:'Heavy snow',77:'Snow grains',80:'Showers',81:'Rain showers',82:'Heavy showers',85:'Snow showers',86:'Heavy snow showers',95:'Thunderstorm',96:'Thunderstorm w/ hail',99:'Severe thunderstorm'};
function _wdesc(code) { return _WMO[code] || (code != null ? 'Code '+code : '–'); }
function _windDir(deg) { if(deg==null) return ''; const d=['N','NE','E','SE','S','SW','W','NW']; return d[Math.round(deg/45)%8]; }

function fmtAge(ts) {
  const s = Math.round(Date.now()/1000 - ts);
  if (s < 5) return 'just now';
  if (s < 60) return s + 's ago';
  return Math.round(s/60) + 'm ago';
}
function fmtAlt(ft) { return ft >= 1000 ? (ft/1000).toFixed(0)+'k ft' : ft+' ft'; }

// ── Leaflet satellite map ──────────────────────────────────────
const MAP_LAT = {{ location_lat }}, MAP_LON = {{ location_lon }};
let _map = null, _rangeCircle = null, _planeMarkers = {}, _centerMark = null;

function initRadarMap() {
  if (_map) return;
  _map = L.map('radarMap', {
    center: [MAP_LAT, MAP_LON],
    zoom: 10,
    zoomControl: true,
    attributionControl: false,
    scrollWheelZoom: true
  });
  // ESRI World Imagery — free satellite tiles, no API key
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 18
  }).addTo(_map);
  // 15-mile range ring
  _rangeCircle = L.circle([MAP_LAT, MAP_LON], {
    radius: 15 * 1609.34,
    color: 'rgba(0,180,255,0.5)',
    weight: 1,
    fill: false
  }).addTo(_map);
  // Center marker (you)
  _centerMark = L.circleMarker([MAP_LAT, MAP_LON], {
    radius: 5, color: '#00ff88', fillColor: '#00ff88', fillOpacity: 1, weight: 2
  }).addTo(_map);
}

function makePlaneIcon(heading, callsign, altitude, isHeli) {
  const alt = altitude
    ? (altitude >= 10000 ? Math.round(altitude/1000)+'k' : altitude) + 'ft'
    : '';
  const color = isHeli ? '#00e0b0' : '#00ccff';
  const glow = `drop-shadow(0 0 3px rgba(0,0,0,0.95)) drop-shadow(0 0 5px ${color}55)`;
  const mono = `font-family:'Share Tech Mono',monospace`;

  // Both icons drawn pointing NORTH (up = 0°). SVG rotate(heading) spins around the
  // viewBox origin, which is the center of the image — perfect heading accuracy.
  let svgHtml, W, H;
  if (isHeli) {
    W = 26; H = 26;
    // Top-down helicopter: rotor disc + oval body (nose up) + tail boom + rotor blade
    svgHtml = `<svg viewBox="-11 -11 22 22" width="${W}" height="${H}"
        style="display:block;overflow:visible;filter:${glow}">
      <g transform="rotate(${heading})">
        <circle r="10" fill="${color}14" stroke="${color}" stroke-width="0.6"/>
        <ellipse rx="2.8" ry="5" cy="-0.5" fill="${color}"/>
        <rect x="-1.2" y="4" width="2.4" height="6" fill="${color}" rx="1"/>
        <line x1="-9.5" y1="-0.5" x2="9.5" y2="-0.5"
          stroke="${color}" stroke-width="1.7" stroke-linecap="round"/>
      </g>
    </svg>`;
  } else {
    W = 30; H = 30;
    // Top-down jet: nose at top (y=-13), swept wings (±13), tail fins (±5)
    // Path drawn pointing north; SVG rotate aligns nose to actual heading
    svgHtml = `<svg viewBox="-15 -15 30 30" width="${W}" height="${H}"
        style="display:block;overflow:visible;filter:${glow}">
      <g transform="rotate(${heading})">
        <path d="M0,-13
                 L1.8,-4
                 L13,4   L12.5,6   L2,2
                 L2,9    L5.5,11   L0,13   L-5.5,11   L-2,9
                 L-2,2   L-12.5,6  L-13,4
                 L-1.8,-4 Z"
          fill="${color}" stroke="rgba(0,10,40,0.55)" stroke-width="0.4"
          stroke-linejoin="round"/>
      </g>
    </svg>`;
  }

  return L.divIcon({
    className: 'plane-label',
    html: `<div style="position:relative;width:${W}px;height:${H}px">
      ${svgHtml}
      <div style="position:absolute;top:${H+3}px;left:50%;transform:translateX(-50%);
        background:rgba(0,0,0,.75);padding:1px 5px;border-radius:3px;
        font-size:9px;color:${color};line-height:1.3;white-space:nowrap;${mono}">
        ${callsign}
      </div>
      ${alt ? `<div style="position:absolute;top:${H+15}px;left:50%;transform:translateX(-50%);
        font-size:8px;color:rgba(180,220,255,.65);white-space:nowrap;${mono}">
        ${alt}
      </div>` : ''}
    </div>`,
    iconAnchor: [W/2, H/2],
    iconSize:   [W, H]
  });
}

function updateMapPlanes(planes) {
  if (!document.getElementById('radarMap')) return;
  if (!_map) initRadarMap();
  const seen = new Set();
  (planes || []).forEach(p => {
    if (p.lat == null || p.lon == null) return;
    seen.add(p.callsign);
    const icon = makePlaneIcon(p.heading, p.callsign, p.altitude_ft, p.is_heli);
    const route = [p.origin, p.dest].filter(Boolean).join('→');
    const tip = `${p.callsign} · ${p.distance}mi · ${fmtAlt(p.altitude_ft)}${route ? ' · '+route : ''}`;
    const popupHtml = buildFlightPopupHtml(p);
    if (_planeMarkers[p.callsign]) {
      _planeMarkers[p.callsign].setLatLng([p.lat, p.lon]).setIcon(icon)
        .bindTooltip(tip).setPopupContent(popupHtml);
    } else {
      _planeMarkers[p.callsign] = L.marker([p.lat, p.lon], {icon})
        .bindTooltip(tip, {className:'plane-tooltip', direction:'top', offset:[0,-10]})
        .bindPopup(popupHtml, {
          className: 'plane-popup-leaflet',
          maxWidth: 300, minWidth: 240,
          offset: [0, -12]
        })
        .addTo(_map);
    }
  });
  Object.keys(_planeMarkers).forEach(cs => {
    if (!seen.has(cs)) { _map.removeLayer(_planeMarkers[cs]); delete _planeMarkers[cs]; }
  });
}

function renderRadarRows(planes) {
  const list = document.getElementById('radarList');
  if (!list) return;
  if (!planes || !planes.length) {
    list.innerHTML = '<div class="radar-empty">No aircraft in range</div>';
    return;
  }
  const count = `<div class="radar-count"><span>${planes.length}</span> aircraft in range</div>`;
  const rows = planes.map(p => {
    const route = (p.origin && p.dest) ? `${p.origin}→${p.dest}` : '';
    const faUrl = `https://www.flightaware.com/live/flight/${encodeURIComponent(p.callsign)}`;
    return `<div class="radar-row" data-plane='${JSON.stringify(p).replace(/'/g,"&#39;")}'>
      <a class="radar-cs" href="${faUrl}" target="_blank" rel="noopener"
         title="View ${p.callsign} on FlightAware">${p.callsign}<sup style="font-size:9px;opacity:.5;margin-left:2px">↗</sup></a>
      <span class="radar-dist">${p.distance}mi</span>
      <span class="radar-alt">${fmtAlt(p.altitude_ft)}</span>
      <span class="radar-route">${route}</span>
    </div>`;
  }).join('');
  list.innerHTML = count + rows;

  // Attach hover handlers now that rows are in the DOM
  list.querySelectorAll('.radar-row').forEach(row => {
    const p = JSON.parse(row.dataset.plane);
    row.addEventListener('mouseenter', () => showFlightPopup(p, row));
    row.addEventListener('mouseleave', hideFlightPopup);
  });
}

function renderISSCard(iss) {
  if (!iss || iss.distance === null) return '<div class="radar-empty">Waiting for first poll…</div>';
  const maxDist = 4000;
  const pct = Math.max(0, Math.min(100, 100 - (iss.distance / maxDist * 100)));
  const isClose = iss.distance < 800;
  const color = isClose ? 'var(--green)' : 'var(--blue)';
  let html = `
    <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:10px">
      <div class="iss-distance">${iss.distance.toLocaleString()}</div>
      <div class="iss-unit">mi away</div>
    </div>
    <div style="background:var(--border);border-radius:2px;height:3px;margin-bottom:10px;overflow:hidden">
      <div style="width:${pct}%;height:100%;background:${color};border-radius:2px;transition:width .5s"></div>
    </div>`;
  if (iss.overhead) html += `<div class="iss-overhead">● OVERHEAD NOW</div>`;
  if (!iss.fresh)   html += `<div class="iss-stale">⚠ Data stale</div>`;
  if (iss.lat !== null) html += `<div style="margin-top:8px;font-family:'Share Tech Mono',monospace;font-size:15px;color:var(--muted);letter-spacing:.5px">
    ${iss.lat > 0 ? iss.lat+'°N' : Math.abs(iss.lat)+'°S'} · ${iss.lon > 0 ? iss.lon+'°E' : Math.abs(iss.lon)+'°W'}
  </div>`;
  return html;
}

async function pollLiveData() {
  try {
    const d = await (await fetch('/api/live_data')).json();
    const age = d.updated ? Math.round(Date.now()/1000 - d.updated) : 999;

    // Satellite map + radar list
    _lastLiveData = d;
    updateMapPlanes(d.planes);
    renderRadarRows(d.planes);

    // ISS
    const ic = document.getElementById('issCard');
    if (ic) ic.innerHTML = renderISSCard(d.iss);

    // Weather monitor
    const w = d.weather;
    const dotW = document.getElementById('dotWeather');
    if (w && w.temp !== null) {
      document.getElementById('monWeatherTemp').textContent = w.temp + '°F';
      const feelsEl = document.getElementById('monWeatherFeels');
      if (feelsEl) feelsEl.textContent = w.feels != null && w.feels !== w.temp ? 'Feels ' + w.feels + '°' : '';
      document.getElementById('monWeatherDesc').textContent = _wdesc(w.code);
      const statsEl = document.getElementById('monWeatherStats');
      if (statsEl) {
        statsEl.style.display = 'grid';
        document.getElementById('monWeatherHumid').textContent  = w.humidity != null ? '💧 ' + w.humidity + '%' : '';
        document.getElementById('monWeatherWind').textContent   = w.wind_speed != null ? '💨 ' + w.wind_speed + ' mph ' + _windDir(w.wind_dir) : '';
        document.getElementById('monWeatherUV').textContent     = w.uv != null ? '☀ UV ' + w.uv : '';
        document.getElementById('monWeatherPrecip').textContent = w.precip != null && w.precip > 0 ? '🌧 ' + w.precip + ' in' : '';
      }
      document.getElementById('monWeatherAge').textContent = age < 120 ? '↻ ' + fmtAge(d.updated) : 'stale';
      if (dotW) dotW.className = 'monitor-dot ok';
    } else {
      document.getElementById('monWeatherTemp').textContent = '–';
      const feelsEl = document.getElementById('monWeatherFeels'); if (feelsEl) feelsEl.textContent = '';
      document.getElementById('monWeatherDesc').textContent = 'no data';
      const statsEl = document.getElementById('monWeatherStats'); if (statsEl) statsEl.style.display = 'none';
      document.getElementById('monWeatherAge').textContent = '';
      if (dotW) dotW.className = 'monitor-dot err';
    }

    // Music monitor
    const m = d.music;
    const dotM = document.getElementById('dotMusic');
    const artImg = document.getElementById('monMusicArt');
    if (m && m.track) {
      document.getElementById('monMusicTrack').textContent = m.track;
      document.getElementById('monMusicArtist').textContent = m.artist || '';
      document.getElementById('monMusicSource').textContent = m.source === 'homepod' ? 'HomePod' : m.source === 'appletv' ? 'Apple TV' : 'Spotify';
      if (dotM) dotM.className = 'monitor-dot ok';
      if (artImg) { artImg.src = '/api/current_art?t=' + Math.round(Date.now()/2000); artImg.style.display = ''; }
    } else {
      document.getElementById('monMusicTrack').textContent = 'Idle';
      document.getElementById('monMusicArtist').textContent = 'Nothing playing';
      document.getElementById('monMusicSource').textContent = '';
      if (dotM) dotM.className = 'monitor-dot';
      if (artImg) artImg.style.display = 'none';
    }
    updateNowOnDisplay();
  } catch(e) {}
}

// Upload
const fi = document.getElementById('fileInput');
const dz = document.getElementById('uploadZone');
const ul = document.getElementById('uploadLabel');
fi.addEventListener('change', () => {
  if (fi.files.length) { ul.textContent = fi.files.length + ' photo' + (fi.files.length!==1?'s':'') + ' ready'; dz.classList.add('active'); }
  else { ul.textContent = 'Tap to add photos'; dz.classList.remove('active'); }
});
async function submitUpload(e) {
  e.preventDefault();
  if (!fi.files.length) { showToast('No files selected', false); return; }
  const btn = e.submitter; btn.disabled = true; btn.textContent = 'Uploading…';
  try {
    const d = await (await fetch('/upload_json', {method:'POST', body:new FormData(e.target)})).json();
    if (d.saved > 0) {
      showToast(d.saved + ' photo' + (d.saved!==1?'s':'') + ' added');
      ul.textContent = 'Tap to add photos'; dz.classList.remove('active'); fi.value = '';
      loadPhotos();
    } else showToast('No valid images found', false);
  } catch { showToast('Upload failed', false); }
  btn.disabled = false; btn.textContent = 'Upload to Dashboard';
}

// Photo management
const grid  = document.getElementById('photoGrid');
const countL = document.getElementById('photoCountLabel');
const delAll = document.getElementById('deleteAllBtn');

let currentForcedPhoto = null;
let currentForcedMode  = null;
let pendingEditName    = null;
let photoSettings      = {};

async function loadPhotoSettings() {
  try { photoSettings = await (await fetch('/api/photo_settings')).json(); } catch {}
}

function epVal(id, v, unit) {
  document.getElementById(id).textContent = parseFloat(v).toFixed(unit==='px'?0:1) + unit;
}

let _epRefreshTimer = null;
function epRefresh() {
  clearTimeout(_epRefreshTimer);
  _epRefreshTimer = setTimeout(() => {
    if (!pendingEditName) return;
    const z = document.getElementById('epZoom').value;
    const x = document.getElementById('epX').value;
    const y = document.getElementById('epY').value;
    const b = document.getElementById('epBright').value;
    document.getElementById('epPreview').src =
      '/api/photo_preview/' + encodeURIComponent(pendingEditName) +
      '?zoom=' + z + '&x=' + x + '&y=' + y + '&brightness=' + b + '&t=' + Date.now();
  }, 120);
}

function openEditPopup(name, tile) {
  pendingEditName = name;
  const s = photoSettings[name] || {};
  const setSlider = (id, valId, val, unit) => {
    document.getElementById(id).value = val;
    epVal(valId, val, unit);
  };
  setSlider('epZoom',  'epZoomVal',  s.zoom       ?? 1.0, 'x');
  setSlider('epX',     'epXVal',     s.x          ?? 0,   'px');
  setSlider('epY',     'epYVal',     s.y          ?? 0,   'px');
  setSlider('epBright','epBrightVal',s.brightness ?? 1.0, 'x');
  epRefresh();
  const popup = document.getElementById('editPopup');
  popup.classList.add('open');
  const r = tile.getBoundingClientRect();
  const pw = popup.offsetWidth, ph = popup.offsetHeight;
  let left = r.left + r.width / 2 - pw / 2;
  let top  = r.top  - ph - 8;
  if (top < 6) top = r.bottom + 8;
  left = Math.max(6, Math.min(left, window.innerWidth - pw - 6));
  top  = Math.max(6, Math.min(top,  window.innerHeight - ph - 6));
  popup.style.left = left + 'px';
  popup.style.top  = top  + 'px';
}

function closeEditPopup() {
  document.getElementById('editPopup').classList.remove('open');
  pendingEditName = null;
}

async function saveEditPopup() {
  const name = pendingEditName; if (!name) return;
  const body = {
    name,
    zoom:       parseFloat(document.getElementById('epZoom').value),
    x:          parseInt(document.getElementById('epX').value),
    y:          parseInt(document.getElementById('epY').value),
    brightness: parseFloat(document.getElementById('epBright').value),
  };
  await fetch('/api/photo_settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  photoSettings[name] = body;
  closeEditPopup();
  showToast('Settings saved');
}

document.addEventListener('click', e => {
  const ep = document.getElementById('editPopup');
  if (ep.classList.contains('open') && !ep.contains(e.target) && !e.target.closest('.photo-edit')) closeEditPopup();
});

function updateForcedPhotoUI(name, mode) {
  currentForcedPhoto = name || null;
  currentForcedMode  = mode || null;
  const st = document.getElementById('stForced');
  if (st) {
    st.textContent = name ? name + (mode === 'freeze' ? ' (frozen)' : ' (slideshow)') : 'none';
    st.className = 'status-val ' + (name ? 'status-warn' : '');
  }
  const banner = document.getElementById('forcedBanner');
  if (name) {
    document.getElementById('forcedBannerLabel').textContent = (mode === 'freeze' ? 'Showing: ' : 'In slideshow: ') + name;
    document.getElementById('forcedBannerDot').className = 'forced-banner-dot' + (mode === 'slideshow' ? ' soft' : '');
    banner.style.borderColor = mode === 'freeze' ? 'rgba(0,204,255,.3)' : 'rgba(0,255,136,.3)';
    banner.style.background  = mode === 'freeze' ? 'rgba(0,204,255,.1)' : 'rgba(0,255,136,.07)';
    banner.classList.add('show');
  } else {
    banner.classList.remove('show');
  }
  document.querySelectorAll('.photo-tile[data-name]').forEach(tile => {
    const is = tile.dataset.name === name;
    tile.classList.toggle('showing',      is && mode === 'freeze');
    tile.classList.toggle('showing-soft', is && mode === 'slideshow');
  });
}

function openPhotoModal(name) {
  document.getElementById('modalFilename').textContent = name;
  document.getElementById('photoOverlay').dataset.photo = name;
  document.getElementById('photoOverlay').classList.add('open');
}

function closeShowModal() {
  document.getElementById('photoOverlay').classList.remove('open');
  delete document.getElementById('photoOverlay').dataset.photo;
}

async function confirmShowPhoto(mode) {
  const name = document.getElementById('photoOverlay').dataset.photo;
  closeShowModal();
  if (!name) { showToast('No photo selected', false); return; }
  try {
    const r = await fetch('/api/force_photo', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, mode})
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    if (mode === 'freeze') {
      updateForcedPhotoUI(name, 'freeze');
      updateSlideUI('photos');
      showToast('Showing on display');
    } else {
      updateForcedPhotoUI(name, 'slideshow');
      showToast('Added to slideshow');
    }
  } catch(e) {
    showToast('Failed: ' + e.message, false);
  }
}

async function clearForcedPhoto() {
  try {
    await fetch('/api/force_photo', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({clear:true})});
    updateForcedPhotoUI(null, null);
    updateSlideUI(null);
    showToast('Stopped — slideshow resumed');
  } catch(e) { showToast('Failed', false); }
}

async function loadPhotos() {
  try {
    const photos = await (await fetch('/api/photos')).json();
    countL.textContent = photos.length + ' photo' + (photos.length!==1?'s':'') + ' in dashboard';
    delAll.style.display = photos.length ? '' : 'none';
    grid.innerHTML = '';
    if (!photos.length) { grid.innerHTML = '<div class="photo-empty" style="grid-column:1/-1">No photos yet</div>'; return; }
    photos.forEach(name => {
      const tile = document.createElement('div');
      tile.className = 'photo-tile'; tile.dataset.name = name;
      if (name === currentForcedPhoto && currentForcedMode === 'freeze')    tile.classList.add('showing');
      if (name === currentForcedPhoto && currentForcedMode === 'slideshow') tile.classList.add('showing-soft');
      const img = document.createElement('img');
      img.src = '/photo/' + encodeURIComponent(name); img.loading = 'lazy';
      tile.addEventListener('click', () => openPhotoModal(name));
      const delBtn = document.createElement('button');
      delBtn.className = 'photo-delete'; delBtn.title = 'Delete'; delBtn.textContent = '✕';
      delBtn.addEventListener('click', e => { e.stopPropagation(); deletePhoto(name); });
      const editBtn = document.createElement('button');
      editBtn.className = 'photo-edit'; editBtn.title = 'Edit';
      editBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
      editBtn.addEventListener('click', e => { e.stopPropagation(); openEditPopup(name, tile); });
      tile.appendChild(img); tile.appendChild(delBtn); tile.appendChild(editBtn);
      grid.appendChild(tile);
    });
  } catch { countL.textContent = 'Could not load photos'; }
}
async function deletePhoto(name) {
  const tile = grid.querySelector('.photo-tile[data-name="' + CSS.escape(name) + '"]');
  if (tile) tile.style.opacity = '0.3';
  try {
    const r = await fetch('/photo/delete/' + encodeURIComponent(name), {method:'POST'});
    if (r.ok) { showToast('Photo deleted'); loadPhotos(); }
    else { showToast('Could not delete photo', false); if (tile) tile.style.opacity = ''; }
  } catch { showToast('Could not delete photo', false); if (tile) tile.style.opacity = ''; }
}
async function deleteAllPhotos() {
  if (!confirm('Delete all ' + (await (await fetch('/api/photos')).json()).length + ' photos?')) return;
  if ((await fetch('/photos/clear', {method:'POST'})).ok) { showToast('All photos deleted'); loadPhotos(); }
  else showToast('Could not delete photos', false);
}

// System status
async function loadStatus() {
  try {
    const d = await (await fetch('/api/status')).json();
    const stDash = document.getElementById('stDash');
    if (stDash) { stDash.textContent = d.running ? 'RUNNING (PID '+d.pid+')' : 'STOPPED'; stDash.className = 'status-val '+(d.running?'status-ok':'status-err'); }
    const stTemp = document.getElementById('stTemp');
    if (stTemp && d.cpu_temp != null) {
      const t = d.cpu_temp;
      stTemp.textContent = t + '°C';
      stTemp.className = 'status-val ' + (t >= 80 ? 'status-err' : t >= 65 ? 'status-warn' : 'status-ok');
    }
    const stCache = document.getElementById('stCache');
    if (stCache) { stCache.textContent = d.cache.files+' files · '+d.cache.mb+' MB'; stCache.className = 'status-val '+(d.cache.mb>40?'status-warn':'status-ok'); }
  } catch(e) {}
}
async function clearArtCache() {
  const d = await (await fetch('/api/clear_art_cache', {method:'POST'})).json();
  showToast('Cleared ' + d.deleted + ' art files');
  loadStatus();
}

async function restartService(which) {
  try {
    showToast('Restarting ' + (which === 'display' ? 'display' : 'web') + '…');
    await fetch('/api/restart/' + which, {method:'POST'});
    if (which === 'web') {
      // page will go down briefly, reload after delay
      setTimeout(() => location.reload(), 3500);
    } else {
      setTimeout(loadStatus, 3000);
    }
  } catch(e) {
    if (which === 'web') setTimeout(() => location.reload(), 3500);
  }
}

// ── Clock style (single Eastern vs 4 zones) ──────────────────────────────
let clockOptions = [];     // [[name, label, tz], ...]
let clockMode = 'single';
let clockZones = [];       // [[label, tz], ...4]

async function loadClockSettings() {
  try {
    const r = await fetch('/api/clock_settings');
    const d = await r.json();
    clockOptions = d.options || [];
    clockMode = d.mode || 'single';
    clockZones = d.zones || [];
    // Populate the four dropdowns from the option list
    for (let i = 0; i < 4; i++) {
      const sel = document.getElementById('clkTz' + i);
      if (!sel) continue;
      sel.innerHTML = clockOptions.map(o =>
        `<option value="${o[1]}|${o[2]}">${o[0]}</option>`).join('');
      const z = clockZones[i];
      if (z) sel.value = z[0] + '|' + z[1];
    }
    renderClockMode();
  } catch (e) {}
}

function renderClockMode() {
  const single = document.getElementById('clkModeSingle');
  const quad = document.getElementById('clkModeQuad');
  const zones = document.getElementById('clkZones');
  if (single) single.classList.toggle('active', clockMode === 'single');
  if (quad) quad.classList.toggle('active', clockMode === 'quad');
  if (zones) zones.style.display = (clockMode === 'quad') ? 'block' : 'none';
}

function _collectClockZones() {
  const out = [];
  for (let i = 0; i < 4; i++) {
    const sel = document.getElementById('clkTz' + i);
    const [label, tz] = (sel ? sel.value : '|').split('|');
    out.push([label, tz]);
  }
  return out;
}

async function _saveClock() {
  await fetch('/api/clock_settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode: clockMode, zones: _collectClockZones()})});
}

async function setClockMode(mode) {
  clockMode = mode;
  renderClockMode();
  await _saveClock();
  showToast(mode === 'single' ? 'Clock: single Eastern' : 'Clock: 4 time zones');
}

async function saveClockZones() {
  await _saveClock();
  showToast('Time zones saved');
}

// Init
pollOverride(); pollFlight(); loadPhotos(); loadStatus(); pollLiveData(); loadPhotoSettings(); loadSchedule(); loadClockSettings();
initRadarMap();
setInterval(pollOverride, 3000);
setInterval(pollFlight, 2000);
setInterval(pollLiveData, 2000);
setInterval(loadStatus, 30000);

// ── Flight detail hover popup ────────────────────────────────────────────────
function _fmtCoord(val, posLetter, negLetter) {
  if (val == null) return '';
  return Math.abs(val).toFixed(4) + '°' + (val >= 0 ? posLetter : negLetter);
}

function buildFlightPopupHtml(p) {
  const hasRoute  = p.origin || p.dest;
  const hasCities = p.origin_city || p.dest_city;
  const faUrl     = `https://www.flightaware.com/live/flight/${encodeURIComponent(p.callsign)}`;

  let html = `<div class="fp-header">`;
  if (p.airline_icao) {
    html += `<img class="fp-logo" src="/api/airline_logo/${encodeURIComponent(p.airline_icao)}"
      onerror="this.style.display='none'" alt="">`;
  }
  html += `<div><div class="fp-cs">${p.callsign}</div>`;
  if (p.airline_name) html += `<div class="fp-airline">${p.airline_name}</div>`;
  html += `</div></div>`;

  if (hasRoute) {
    html += `<hr class="fp-divider">`;
    const routeStr = [p.origin || '?', p.dest || '?'].join(' → ');
    html += `<div class="fp-route">${routeStr}</div>`;
    if (hasCities) {
      const citiesStr = [p.origin_city, p.dest_city].filter(Boolean).join(' → ');
      html += `<div class="fp-cities">${citiesStr}</div>`;
    }
  }

  html += `<hr class="fp-divider"><div class="fp-stats">`;
  if (p.type)         html += `<span class="fp-stat">✈ ${p.type}</span>`;
  if (p.altitude_ft)  html += `<span class="fp-stat">${fmtAlt(p.altitude_ft)}</span>`;
  if (p.speed_mph)    html += `<span class="fp-stat">${Math.round(p.speed_mph)} mph</span>`;
  if (p.heading != null) html += `<span class="fp-stat">HDG ${p.heading}°</span>`;
  html += `</div>`;

  html += `<div class="fp-dist">📍 ${p.distance} mi away</div>`;

  if (p.lat != null) {
    html += `<div class="fp-coords">${_fmtCoord(p.lat,'N','S')} · ${_fmtCoord(p.lon,'E','W')}</div>`;
  }
  if (p.registration) html += `<div class="fp-reg">Reg: ${p.registration}</div>`;

  html += `<a class="fp-fa" href="${faUrl}" target="_blank" rel="noopener">View on FlightAware ↗</a>`;
  return html;
}

let _popupHideTimer = null;
function showFlightPopup(p, anchorEl) {
  clearTimeout(_popupHideTimer);
  const popup = document.getElementById('flightPopup');
  popup.innerHTML = buildFlightPopupHtml(p);
  popup.style.display = 'block';
  // Position: right of the element, flip left if too close to edge
  const rect = anchorEl.getBoundingClientRect();
  const pw = 290;
  let left = rect.right + 10;
  if (left + pw > window.innerWidth - 8) left = rect.left - pw - 10;
  let top = rect.top - 10;
  const maxTop = window.innerHeight - popup.offsetHeight - 8;
  if (top > maxTop) top = maxTop;
  if (top < 8) top = 8;
  popup.style.left = Math.max(8, left) + 'px';
  popup.style.top  = top + 'px';
}
function hideFlightPopup() {
  _popupHideTimer = setTimeout(() => {
    document.getElementById('flightPopup').style.display = 'none';
  }, 120);
}
</script>
<!-- Flight detail popup (singleton, positioned by JS) -->
<div id="flightPopup"></div>
</body>
</html>"""

# Routes

_TMPL_VARS = dict(title=WEB_TITLE, location_lat=LOCATION_LAT, location_lon=LOCATION_LON)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            session["authed"] = True; return redirect("/")
        return render_template_string(LOGIN_HTML, error=True, **_TMPL_VARS)
    if session.get("authed"): return redirect("/")
    return render_template_string(LOGIN_HTML, error=False, **_TMPL_VARS)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear(); return redirect("/login")

@app.route("/")
@login_required
def index():
    return render_template_string(HTML, **_TMPL_VARS)

@app.route("/track", methods=["POST"])
@login_required
def track():
    cs = request.form.get("callsign","").upper().strip()
    if not cs: return ('',400)
    set_manual_track(cs); return ('',204)

@app.route("/track/clear", methods=["POST"])
@login_required
def track_clear():
    set_manual_track(None); return ('',204)

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    files = request.files.getlist("photos")
    if not files or all(f.filename=="" for f in files): return redirect("/?err=no_files")
    saved = 0
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS: continue
        f.save(os.path.join(PHOTOS_DIR, f"{int(time.time()*1000)}_{saved}{ext}")); saved += 1
    return redirect("/")

@app.route("/upload_json", methods=["POST"])
@login_required
def upload_json():
    files = request.files.getlist("photos"); saved = 0
    for f in files:
        if not f.filename: continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS: continue
        f.save(os.path.join(PHOTOS_DIR, f"{int(time.time()*1000)}_{saved}{ext}")); saved += 1
    return jsonify({"saved": saved})

@app.route("/photo/<path:filename>")
@login_required
def serve_photo(filename):
    safe = os.path.basename(filename)
    if not os.path.exists(os.path.join(PHOTOS_DIR, safe)): abort(404)
    return send_from_directory(PHOTOS_DIR, safe)

@app.route("/photo/delete/<path:filename>", methods=["POST"])
@login_required
def delete_photo(filename):
    safe = os.path.basename(filename)
    full = os.path.join(PHOTOS_DIR, safe)
    if not os.path.exists(full): abort(404)
    os.remove(full); return ('',204)

@app.route("/photos/clear", methods=["POST"])
@login_required
def clear_photos():
    for f in list_photos():
        try: os.remove(os.path.join(PHOTOS_DIR, f))
        except: pass
    return ('',204)

@app.route("/api/photos")
@login_required
def api_photos():
    return jsonify(list_photos())

@app.route("/api/track")
@login_required
def api_track():
    return jsonify(get_manual_track())

@app.route("/api/override")
@login_required
def api_override():
    ov = get_override()
    import datetime as _dt
    _now = _dt.datetime.now()
    t = _now.hour + _now.minute / 60.0
    sched = get_bright_schedule()
    segs = sched.get("segments", [])
    sched_pct = 100
    if segs:
        for seg in segs:
            if t < seg.get("end", 24):
                sched_pct = seg["bright"]; break
        else:
            sched_pct = segs[-1]["bright"]
    manual = ov.get("brightness")
    ov["effective_pct"] = round(manual * 100) if manual is not None else sched_pct
    ov["schedule"] = sched
    return jsonify(ov)

@app.route("/api/bright_schedule", methods=["GET","POST"])
@login_required
def api_bright_schedule():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        # ── New segments format ──
        if "segments" in data:
            raw = data["segments"]
            if not isinstance(raw, list) or not raw:
                return ('', 400)
            cleaned = []
            for i, s in enumerate(raw):
                entry = {"bright": max(0, min(100, int(s.get("bright", 0))))}
                if i < len(raw) - 1:          # all but the last have an 'end'
                    v = round(float(s.get("end", 12)) * 2) / 2
                    entry["end"] = max(0.5, min(23.5, v))
                cleaned.append(entry)
            tmp = BRIGHT_SCHEDULE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"segments": cleaned}, f)
            os.replace(tmp, BRIGHT_SCHEDULE_FILE)
            return ('', 204)
        # ── Legacy 4-key format (backward compat) ──
        sched = {}
        for k in _DEFAULT_SCHEDULE:
            if k in data:
                if "bright" in k:
                    sched[k] = max(0, min(100, int(data[k])))
                else:
                    v = round(float(data[k]) * 2) / 2
                    sched[k] = max(0.5, min(23.5, v))
        with open(BRIGHT_SCHEDULE_FILE, "w") as f:
            json.dump({**_DEFAULT_SCHEDULE, **sched}, f)
        return ('', 204)
    return jsonify(get_bright_schedule())

@app.route("/api/clock_settings", methods=["GET", "POST"])
@login_required
def api_clock_settings():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        mode = data.get("mode")
        if mode not in ("single", "quad"):
            return ('', 400)
        out = {"mode": mode}
        # Validate the 4 zones against the known timezone list; fall back to the
        # default for any slot that's missing or unrecognised.
        zones_in = data.get("zones") or []
        zones = []
        for i in range(4):
            try:
                lbl, tz = str(zones_in[i][0])[:4], str(zones_in[i][1])
            except Exception:
                lbl, tz = _DEFAULT_CLOCK_ZONES[i]
            if tz not in _VALID_TZS:
                lbl, tz = _DEFAULT_CLOCK_ZONES[i]
            zones.append([lbl, tz])
        out["zones"] = zones
        tmp = CLOCK_SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, CLOCK_SETTINGS_FILE)
        return ('', 204)
    return jsonify({**get_clock_settings(), "options": CLOCK_TZ_OPTIONS})

@app.route("/api/brightness", methods=["POST"])
@login_required
def api_brightness():
    data = request.get_json(force=True)
    if data.get("auto"): set_override(brightness=None)
    else: set_override(brightness=max(0.0, min(1.0, float(data.get("value",1.0)))))
    return ('',204)

@app.route("/api/slide", methods=["POST"])
@login_required
def api_slide():
    data = request.get_json(force=True)
    if data.get("auto"): set_override(slide_lock=None, forced_photo=None)
    elif data.get("lock") in ("clock","weather","sun","photos","flights","iss"): set_override(slide_lock=data["lock"])
    return ('',204)

@app.route("/api/slide_disabled", methods=["POST"])
@login_required
def api_slide_disabled():
    data = request.get_json(force=True)
    disabled = [s for s in (data.get("disabled") or []) if s in ("clock","weather","sun","photos","flights","iss")]
    set_override(disabled_slides=disabled if disabled else None)
    return ('', 204)

@app.route("/api/force_photo", methods=["POST"])
@login_required
def api_force_photo():
    data = request.get_json(force=True)
    if data.get("clear"):
        set_override(forced_photo=None, slide_lock=None)
    else:
        name = os.path.basename(data.get("name",""))
        if name:
            if data.get("mode") == "slideshow":
                set_override(forced_photo=name, slide_lock=None)  # clear lock, normal cycle
            else:
                set_override(forced_photo=name, slide_lock="photos")  # freeze on photos
    return ('',204)

@app.route("/api/photo_settings", methods=["GET"])
@login_required
def api_photo_settings_get():
    return jsonify(get_photo_settings())

@app.route("/api/photo_settings", methods=["POST"])
@login_required
def api_photo_settings_save():
    data = request.get_json(force=True)
    name = os.path.basename(data.get("name", ""))
    if not name: return ('', 400)
    settings = get_photo_settings()
    settings[name] = {
        "zoom":       float(data.get("zoom", 1.0)),
        "x":          int(data.get("x", 0)),
        "y":          int(data.get("y", 0)),
        "brightness": float(data.get("brightness", 1.0)),
    }
    save_photo_settings(settings)
    return ('', 204)

@app.route("/api/photo_preview/<path:filename>")
@login_required
def api_photo_preview(filename):
    try:
        from PIL import Image, ImageEnhance
        safe = os.path.basename(filename)
        path = os.path.join(PHOTOS_DIR, safe)
        if not os.path.exists(path): abort(404)
        zoom   = float(request.args.get("zoom",   1.0))
        ox     = int(request.args.get("x",         0))
        oy     = int(request.args.get("y",         0))
        bright = float(request.args.get("brightness", 1.0))
        img = Image.open(path).convert("RGB")
        w, h = img.size
        scale = max(64.0 / w, 64.0 / h) * zoom
        nw, nh = max(int(w * scale + 0.5), 1), max(int(h * scale + 0.5), 1)
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        left = max(0, min((nw - 64) // 2 - ox, max(nw - 64, 0)))
        top  = max(0, min((nh - 64) // 2 - oy, max(nh - 64, 0)))
        img = img.crop((left, top, left + 64, top + 64))
        if img.size != (64, 64): img = img.resize((64, 64), Image.Resampling.LANCZOS)
        if bright != 1.0: img = ImageEnhance.Brightness(img).enhance(bright)
        img = img.resize((128, 128), Image.Resampling.NEAREST)
        buf = BytesIO(); img.save(buf, "JPEG", quality=85); buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception as e:
        abort(500)

@app.route("/api/display_preview")
@login_required
def api_display_preview():
    try:
        data = get_live_data()
        ov = get_override()
        lock = ov.get("slide_lock") or "auto"
        forced = ov.get("forced_photo")
        if forced and lock == "photos":
            path = os.path.join(PHOTOS_DIR, os.path.basename(forced))
            if os.path.exists(path):
                from PIL import Image
                img = Image.open(path).convert("RGB")
                w, h = img.size
                scale = max(64.0/w, 64.0/h)
                nw, nh = max(int(w*scale),1), max(int(h*scale),1)
                img = img.resize((nw,nh), Image.Resampling.LANCZOS)
                img = img.crop(((nw-64)//2, (nh-64)//2, (nw-64)//2+64, (nh-64)//2+64))
                img = img.resize((128,128), Image.Resampling.NEAREST)
                buf = BytesIO(); img.save(buf,"JPEG",quality=90); buf.seek(0)
                return send_file(buf, mimetype="image/jpeg")
    except Exception:
        pass
    abort(204)

@app.route("/api/live_data")
@login_required
def api_live_data():
    return jsonify(get_live_data())

@app.route("/api/status")
@login_required
def api_status():
    pid = get_dashboard_pid()
    return jsonify({"running": pid is not None, "pid": pid, "cache": get_art_cache_info(),
                    "cpu_temp": get_cpu_temp()})

@app.route("/api/clear_art_cache", methods=["POST"])
@login_required
def api_clear_art_cache():
    deleted = 0
    try:
        for f in os.listdir(CACHE_DIR):
            if any(f.startswith(p) for p in ART_PREFIXES):
                try: os.remove(os.path.join(CACHE_DIR, f)); deleted += 1
                except: pass
    except: pass
    return jsonify({"deleted": deleted})

@app.route("/api/current_art")
@login_required
def api_current_art():
    try:
        files = [
            os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)
            if any(f.startswith(p) for p in ART_PREFIXES)
               and f.lower().endswith((".jpg",".jpeg",".png",".webp"))
        ]
        if not files:
            return ('', 404)
        latest = max(files, key=os.path.getmtime)
        ext = os.path.splitext(latest)[1].lower()
        mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","webp":"image/webp"}.get(ext.lstrip("."),"image/jpeg")
        return send_file(latest, mimetype=mime, max_age=1)
    except Exception:
        return ('', 404)

@app.route("/api/airline_logo/<icao>")
@login_required
def api_airline_logo(icao):
    """Serve a cached airline logo PNG (pre-warmed by the display service)."""
    safe = "".join(c for c in icao.upper() if c.isalnum())[:6]
    f = os.path.join(CACHE_DIR, "logos", safe + ".png")
    if os.path.exists(f):
        return send_file(f, mimetype="image/png", max_age=86400)
    return ('', 404)

@app.route("/api/restart/<which>", methods=["POST"])
@login_required
def api_restart(which):
    import threading, subprocess
    services = {
        "display": "spotify-display.service",
        "web":     "dashboard-web.service",
    }
    svc = services.get(which)
    if not svc: return ('', 400)
    def _do(s):
        import time as _t
        if s == "dashboard-web.service": _t.sleep(0.3)
        subprocess.run(["systemctl", "restart", s])
    threading.Thread(target=_do, args=(svc,), daemon=True).start()
    return ('', 204)

# ─────────────────────────────────────────────────────────────────────────────
# SPOTIFY OAUTH SETUP  (zero-console first-run flow)
# ─────────────────────────────────────────────────────────────────────────────
_SPOTIFY_CONF = Path(os.path.expanduser("~/.spotify_display.conf"))

_SPOTIFY_SCOPE = "user-read-currently-playing"

_SPOTIFY_SETUP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>{{ title }} – Spotify Setup</title>
<style>
:root{--bg:#08090c;--surface:#111318;--border:#252830;--blue:#3b82f6;--green:#1DB954;--white:#e8eaf0;--sub:#6b7280;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;background:var(--bg);color:var(--white);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:32px 28px;width:100%;max-width:420px;}
.logo{font-size:38px;text-align:center;margin-bottom:10px;}
h1{font-size:21px;font-weight:700;text-align:center;margin-bottom:6px;}
.sub{font-size:13px;color:var(--sub);text-align:center;margin-bottom:24px;}
label{display:block;font-size:13px;font-weight:600;color:var(--sub);margin-bottom:5px;margin-top:16px;}
input{width:100%;background:#1a1d25;border:1px solid var(--border);border-radius:10px;padding:11px 14px;color:var(--white);font-size:15px;outline:none;}
input:focus{border-color:var(--blue);}
.hint{font-size:12px;color:var(--sub);margin-top:6px;}
.hint a{color:var(--blue);text-decoration:none;}
.btn{display:block;width:100%;margin-top:24px;padding:13px;background:var(--green);color:#000;font-weight:700;font-size:16px;border:none;border-radius:12px;cursor:pointer;letter-spacing:.3px;}
.btn:hover{opacity:.88;}
.err{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.35);color:#fca5a5;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:16px;text-align:center;}
.already{background:rgba(29,185,84,.1);border:1px solid rgba(29,185,84,.3);color:#86efac;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:16px;text-align:center;}
</style>
</head>
<body>
<div class="card">
  <div class="logo">🎵</div>
  <h1>Connect Spotify</h1>
  <p class="sub">Authorise once — no console needed</p>

  {% if already %}
  <div class="already">✅ Spotify is already connected. Re-authorise below to replace the token.</div>
  {% endif %}
  {% if error %}
  <div class="err">{{ error }}</div>
  {% endif %}

  <form method="POST">
    <label>Spotify Client ID</label>
    <input name="client_id" type="text" placeholder="e.g. 3abc…" value="{{ prefill_id }}" required autocomplete="off" autocorrect="off" autocapitalize="none">
    <label>Spotify Client Secret</label>
    <input name="client_secret" type="password" placeholder="••••••••••••••••" value="{{ prefill_secret }}" required autocomplete="off">
    <p class="hint">
      Create an app at <a href="https://developer.spotify.com/dashboard" target="_blank">developer.spotify.com</a>,
      then add <code>{{ redirect_uri }}</code> as a Redirect URI.
    </p>
    <button class="btn" type="submit">Connect with Spotify →</button>
  </form>
</div>
</body>
</html>"""

_SPOTIFY_SUCCESS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta http-equiv="refresh" content="4;url=/">
<title>{{ title }} – Spotify Connected</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#08090c;color:#e8eaf0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
.card{background:#111318;border:1px solid #1DB954;border-radius:20px;padding:40px 28px;width:100%;max-width:360px;text-align:center;}
.icon{font-size:52px;margin-bottom:16px;}
h1{font-size:22px;font-weight:700;margin-bottom:8px;color:#1DB954;}
p{font-size:14px;color:#6b7280;}
</style>
</head>
<body>
<div class="card">
  <div class="icon">✅</div>
  <h1>Spotify Connected!</h1>
  <p>Your dashboard will start playing in a moment.<br>Redirecting to dashboard…</p>
</div>
</body>
</html>"""

_SPOTIFY_ERROR_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{ title }} – Spotify Error</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#08090c;color:#e8eaf0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
.card{background:#111318;border:1px solid rgba(239,68,68,.5);border-radius:20px;padding:40px 28px;width:100%;max-width:360px;text-align:center;}
.icon{font-size:48px;margin-bottom:16px;}
h1{font-size:20px;font-weight:700;margin-bottom:8px;color:#f87171;}
p{font-size:13px;color:#6b7280;word-break:break-all;}
a{display:inline-block;margin-top:20px;color:#3b82f6;font-size:14px;}
</style>
</head>
<body>
<div class="card">
  <div class="icon">⚠️</div>
  <h1>Authorisation Failed</h1>
  <p>{{ error }}</p>
  <a href="/spotify/setup">← Try again</a>
</div>
</body>
</html>"""


@app.route("/spotify/setup", methods=["GET", "POST"])
@login_required
def spotify_setup():
    redirect_uri = request.url_root.rstrip("/") + "/spotify/callback"

    if request.method == "POST":
        client_id     = request.form.get("client_id", "").strip()
        client_secret = request.form.get("client_secret", "").strip()
        if not client_id or not client_secret:
            return render_template_string(_SPOTIFY_SETUP_HTML,
                redirect_uri=redirect_uri, error="Both fields are required.",
                already=False, prefill_id=client_id, prefill_secret="",
                **_TMPL_VARS)
        # Store creds in session for callback
        state = _secrets.token_urlsafe(16)
        session["sp_state"]  = state
        session["sp_cid"]    = client_id
        session["sp_csec"]   = client_secret
        auth_url = (
            "https://accounts.spotify.com/authorize"
            f"?client_id={_urlquote(client_id)}"
            f"&response_type=code"
            f"&redirect_uri={_urlquote(redirect_uri)}"
            f"&scope={_urlquote(_SPOTIFY_SCOPE)}"
            f"&state={state}"
        )
        return redirect(auth_url)

    # GET — prefill client_id/secret if already saved
    prefill_id = prefill_secret = ""
    already = False
    if _SPOTIFY_CONF.exists():
        try:
            saved = json.loads(_SPOTIFY_CONF.read_text())
            prefill_id     = saved.get("client_id", "")
            prefill_secret = saved.get("client_secret", "")
            already        = bool(saved.get("refresh_token"))
        except Exception:
            pass

    return render_template_string(_SPOTIFY_SETUP_HTML,
        redirect_uri=redirect_uri, error=None, already=already,
        prefill_id=prefill_id, prefill_secret=prefill_secret,
        **_TMPL_VARS)


@app.route("/spotify/callback")
def spotify_callback():
    """Spotify redirects here after the user authorises (or denies)."""
    error = request.args.get("error")
    if error:
        return render_template_string(_SPOTIFY_ERROR_HTML,
            error=f"Spotify denied access: {error}", **_TMPL_VARS)

    code  = request.args.get("code", "")
    state = request.args.get("state", "")

    if not code or state != session.get("sp_state"):
        return render_template_string(_SPOTIFY_ERROR_HTML,
            error="Invalid state parameter — please try again.", **_TMPL_VARS)

    client_id     = session.pop("sp_cid", "")
    client_secret = session.pop("sp_csec", "")
    session.pop("sp_state", None)
    redirect_uri  = request.url_root.rstrip("/") + "/spotify/callback"

    # Exchange auth code for tokens
    auth_header = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        r = _req.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": redirect_uri,
            },
            timeout=10,
        )
        r.raise_for_status()
        tokens = r.json()
    except Exception as e:
        return render_template_string(_SPOTIFY_ERROR_HTML,
            error=f"Token exchange failed: {e}", **_TMPL_VARS)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return render_template_string(_SPOTIFY_ERROR_HTML,
            error="Spotify did not return a refresh token.", **_TMPL_VARS)

    # Merge into existing config (preserve any other keys like homepod_ids)
    saved = {}
    if _SPOTIFY_CONF.exists():
        try:
            saved = json.loads(_SPOTIFY_CONF.read_text())
        except Exception:
            pass
    saved["client_id"]     = client_id
    saved["client_secret"] = client_secret
    saved["refresh_token"] = refresh_token
    try:
        _SPOTIFY_CONF.write_text(json.dumps(saved, indent=2))
    except Exception as e:
        return render_template_string(_SPOTIFY_ERROR_HTML,
            error=f"Could not save config: {e}", **_TMPL_VARS)

    return render_template_string(_SPOTIFY_SUCCESS_HTML, **_TMPL_VARS)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
