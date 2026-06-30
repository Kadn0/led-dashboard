#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════════════════════════════════
# LED DASHBOARD — Real-time flight tracking, weather, ISS, Spotify on a 64×64 LED matrix
# ════════════════════════════════════════════════════════════════════════════════════════
#
# FEATURES:
#   • Flight tracking: Shows aircraft within 25mi, updates every 10-30s from 3 APIs
#   • ISS tracking: Shows distance to ISS, alerts when overhead (<250mi)
#   • Weather: Current conditions + forecast, fetched every 30min
#   • Music: Displays currently-playing Spotify or HomePod track
#   • Clock: World time in 4 zones (NYC, London, Tokyo, Sydney)
#   • Photos: Slideshow of photos from ~/led-dashboard/photos/
#   • Web UI: Control everything via http://pi-ip:8000 (brightness, manual callsign search, etc)
#
# ARCHITECTURE:
#   • Main loop runs at ~2 frames/sec, rotating through slides (clock 20s → weather 5s → ...)
#   • Flight/weather/ISS data polled in background threads so display never blocks
#   • Overlays (flights, ISS, music) interrupt the slide rotation with highest priority
#   • Plane silhouette transition wipes between slides (non-blocking, runs in thread)
#   • Status file updated every 5s for web dashboard to read
#
# CONFIGURATION:
#   • Location: Hardcoded as Chattanooga, TN (can be auto-detected on startup via IP geolocation)
#   • Timeouts: 8s for flight APIs, 5s for weather/AQI, 15s for mDNS (HomePod discovery)
#   • Display: 64×64 RGB matrix via PPM pipe to external display driver (/usr/local/bin/display-bin)
#   • Web server: Flask on port 8000 (http://pi-ip:8000)
#
# NETWORK REQUIREMENTS:
#   • Internet: Weather, flight APIs, Spotify, ISS position
#   • Local: mDNS (port 5353) for HomePod discovery, Spotify Zeroconf for local auth
# ════════════════════════════════════════════════════════════════════════════════════════
"""
╔════════════════════════════════════════════════════════════════════════════╗
║                    LED DASHBOARD FOR RASPBERRY PI 4                        ║
║                                                                            ║
║  Real-time display showing:                                              ║
║  • Flight tracking (ADS-B data from 3 APIs)                             ║
║  • Weather & AQI                                                          ║
║  • ISS position tracking                                                  ║
║  • Spotify & HomePod music                                               ║
║  • World clock                                                            ║
║  • Photo slideshow                                                        ║
║                                                                            ║
║  Architecture:                                                            ║
║  • Main loop: ~2 fps, never blocks on network (all async/threaded)       ║
║  • Background threads: API polling, image downloads, HomePod discovery   ║
║  • Smart caching: Image, weather, flight data cached to avoid lag       ║
║  • Optimizations: Connection pooling, timeouts, resource cleanup         ║
╚════════════════════════════════════════════════════════════════════════════╝
"""

import os, sys, time, requests, base64, hashlib, json, subprocess, math, threading, asyncio, random, socket
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────────────────────────────────────
# HTTP SESSION WITH CONNECTION POOLING (prevents lag from creating new connections)
# ─────────────────────────────────────────────────────────────────────────
def _create_session():
    """Create requests session with connection pooling and retries."""
    session = requests.Session()
    retry_strategy = Retry(total=2, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

_session = _create_session()  # Reuse across all HTTP calls

try:
    import pyatv
    HAS_PYATV = True
except ImportError:
    HAS_PYATV = False

try:
    import qrcode as _qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False

from dashboard_config import (
    MATRIX_WIDTH, MATRIX_HEIGHT, DISPLAY_BIN,
    LOCATION_LAT as CHATTANOOGA_LAT,
    LOCATION_LON as CHATTANOOGA_LON,
    LOCATION_TZ,
    LOCATION_NAME,          # human-readable city name used as geo fallback
    USER_FACING_DEG,
    DASHBOARD_VERSION, DASHBOARD_CREDIT, DASHBOARD_CREDIT_COLOR,
    CLOCK_TIMEZONES,
    FLIGHT_RADIUS_MILES as RADIUS_MILES,
    FLIGHT_POLL_INTERVAL, PLANE_DISPLAY_DURATION, PLANE_REPEAT_INTERVAL,
    ISS_POLL_INTERVAL, ISS_OVERHEAD_RADIUS_MILES,
    SLOT_DURATION, SLOT_DURATIONS, PHOTO_ROTATE_INTERVAL, INTERRUPT_DURATION,
    WEATHER_POLL_INTERVAL, CLOCK_REFRESH_INTERVAL,
    SPOTIFY_POLL_INTERVAL, HOMEPOD_POLL_INTERVAL,
    WEB_PORT,
    DEFAULT_BRIGHT_SCHEDULE as _DEFAULT_SCHEDULE,
    PHOTOS_DIR as _PHOTOS_DIR_STR,
    CACHE_DIR  as _CACHE_DIR_STR,
    MANUAL_TRACK_FILE, STATUS_FILE, OVERRIDE_FILE, BRIGHT_SCHEDULE_FILE, PID_FILE,
    PHOTO_SETTINGS_FILE as _PHOTO_SETTINGS_FILE_STR,
    CLOCK_SETTINGS_FILE,
)
import dashboard_config as _cfg   # for first-run setup state (setup_is_complete, effective_location, …)

_PID_FILE = PID_FILE
def _acquire_pid_lock():
    """Single-instance guard: if a previous dashboard is still running, signal it
    to exit before we take over the display pipe (two writers = flicker)."""
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as _pf:
                old_pid = int(_pf.read().strip())
            os.kill(old_pid, 0)            # raises if the pid is gone
            print(f"Another dashboard instance (pid {old_pid}) is running. Killing it.")
            os.kill(old_pid, 15)
            time.sleep(1)
        except (ProcessLookupError, ValueError):
            pass                          # stale/garbage pid file — safe to overwrite
        except PermissionError:
            # The pid exists but is owned by another user (we can't signal it).
            # It's clearly a live process, so don't fight it — just record ours and
            # carry on rather than crashing the whole dashboard on startup.
            print(f"Existing pid {old_pid} owned by another user; continuing.")
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

_acquire_pid_lock()

_DEFAULT_SEGMENTS = [
    {"end": 6,  "bright": 0},
    {"end": 10, "bright": 35},
    {"end": 20, "bright": 100},
    {"end": 23, "bright": 35},
    {"bright": 0},
]

# get_brightness() runs twice per displayed frame, and it calls
# get_bright_schedule(). Without caching, that would re-read + JSON-parse the
# schedule file from disk on every frame (~4 reads/sec) — pointless I/O that
# can stutter under load. We cache the parsed schedule for 2s; the web UI only
# changes it occasionally, so a 2s lag in picking up edits is imperceptible.
_bright_schedule_cache = None
_bright_schedule_cache_time = 0.0

def get_bright_schedule():
    """Returns {"segments": [{bright, end?}, ...]} — last entry has no 'end' (→ 24:00).
    Reads the new flexible format, or converts the legacy 4-key format on the fly.
    Result is cached for 2s to avoid per-frame disk reads."""
    global _bright_schedule_cache, _bright_schedule_cache_time
    # Serve from cache if fresh
    if _bright_schedule_cache is not None and time.time() - _bright_schedule_cache_time < 2.0:
        return _bright_schedule_cache
    result = {"segments": list(_DEFAULT_SEGMENTS)}  # safe default
    try:
        if os.path.exists(BRIGHT_SCHEDULE_FILE):
            with open(BRIGHT_SCHEDULE_FILE) as _bsf:
                data = json.loads(_bsf.read())
            if isinstance(data.get("segments"), list) and data["segments"]:
                result = data
            else:
                # Convert legacy 4-period format → segment list
                s = {**_DEFAULT_SCHEDULE, **data}
                result = {"segments": [
                    {"end": float(s["night_end"]),   "bright": int(s["night_bright"])},
                    {"end": float(s["morning_end"]), "bright": int(s["morning_bright"])},
                    {"end": float(s["day_end"]),     "bright": int(s["day_bright"])},
                    {"end": float(s["evening_end"]), "bright": int(s["evening_bright"])},
                    {"bright": int(s["night_bright"])},
                ]}
    except Exception:
        pass  # fall back to default on any parse/read error
    _bright_schedule_cache = result
    _bright_schedule_cache_time = time.time()
    return result

def get_manual_track_callsign():
    try:
        if os.path.exists(MANUAL_TRACK_FILE):
            with open(MANUAL_TRACK_FILE) as _mtf:
                d = json.loads(_mtf.read())
            return d.get("callsign")
    except Exception:
        pass
    return None

def write_track_status(callsign, status, last_searched=None):
    d = {"callsign": callsign, "status": status}
    if last_searched:
        d["last_searched"] = last_searched
    try:
        with open(MANUAL_TRACK_FILE, "w") as f:
            json.dump(d, f)
    except Exception:
        pass

CACHE_DIR        = Path(_CACHE_DIR_STR)
LOGO_CACHE_DIR   = CACHE_DIR / "logos"
PHOTOS_DIR       = Path(_PHOTOS_DIR_STR)
PHOTO_SETTINGS_FILE = Path(_PHOTO_SETTINGS_FILE_STR)
CACHE_DIR.mkdir(exist_ok=True)
LOGO_CACHE_DIR.mkdir(exist_ok=True)
PHOTOS_DIR.mkdir(exist_ok=True)

# Remove stale "miss" marker files from a previous run so fresh logo lookups
# are attempted on the next boot (the miss file blocks retries for that session).
for miss in LOGO_CACHE_DIR.glob("*.miss"):
    try: miss.unlink()
    except Exception: pass

ART_PREFIXES = ("homepod_", "spotify_", "album_", "art_")

def _write_status(music, planes, flights_tracker, iss_tracker, weather):
    try:
        plane_list = []
        for p in (planes or [])[:8]:
            route = flights_tracker.route_cache.get(p["callsign"], {})
            plane_list.append({
                "callsign":     p["callsign"],
                "registration": p.get("registration", ""),
                "lat":          round(p["lat"], 5),
                "lon":          round(p["lon"], 5),
                "distance":     round(p["distance"], 1),
                "altitude_ft":  p["altitude_ft"],
                "heading":      int(p.get("heading", 0)),
                "speed_mph":    p.get("speed_mph", 0),
                "type":         p.get("type", ""),
                "category":     p.get("category", ""),
                "is_heli":      _is_helicopter(p),
                "origin":       route.get("origin"),
                "dest":         route.get("dest"),
                "origin_city":  route.get("origin_city"),
                "dest_city":    route.get("dest_city"),
                "airline_name": route.get("airline_name"),
                "airline_icao": route.get("airline_icao"),
                "airline_iata": route.get("airline_iata"),
            })
        iss_data = None
        if iss_tracker and iss_tracker.distance is not None:
            iss_data = {
                "distance": int(iss_tracker.distance),
                # Use "is not None" — a value of exactly 0.0 (equator / prime
                # meridian) is valid and must not be coerced to None.
                "lat": round(iss_tracker.lat, 2) if iss_tracker.lat is not None else None,
                "lon": round(iss_tracker.lon, 2) if iss_tracker.lon is not None else None,
                "overhead": iss_tracker.distance < ISS_OVERHEAD_RADIUS_MILES and iss_tracker.is_fresh(),
                "fresh": iss_tracker.is_fresh(),
            }
        weather_data = None
        if weather:
            weather_data = {
                "temp":     int(weather["current_temp"])   if weather.get("current_temp")   is not None else None,
                "feels":    int(weather["current_feels"])  if weather.get("current_feels")  is not None else None,
                "code":     weather.get("current_code"),
                "uv":       weather.get("current_uv"),
                "humidity": weather.get("current_humidity"),
                "wind_speed": round(weather["current_wind_speed"], 1) if weather.get("current_wind_speed") is not None else None,
                "wind_dir": weather.get("current_wind_dir"),
                "precip":   weather.get("current_precip"),
            }
        with open(STATUS_FILE, "w") as f:
            json.dump({
                "updated": time.time(),
                "music": {"track": music.get("track"), "artist": music.get("artist"),
                          "source": music.get("source")} if music else None,
                "planes": plane_list,
                "iss": iss_data,
                "weather": weather_data,
            }, f)
    except Exception:
        pass

def cleanup_art_cache(max_files=200, max_mb=50):
    try:
        files = [(f, f.stat().st_mtime, f.stat().st_size)
                 for f in CACHE_DIR.iterdir()
                 if f.is_file() and f.name.startswith(ART_PREFIXES)]
        files.sort(key=lambda x: x[1], reverse=True)
        keep = 0; total = 0
        for _, _, sz in files:
            if keep >= max_files or total + sz > max_mb * 1024 * 1024: break
            keep += 1; total += sz
        for f, _, _ in files[keep:]:
            try: f.unlink()
            except Exception: pass
        if len(files) - keep > 0:
            print(f"Art cache: removed {len(files)-keep} old files, kept {keep} ({total//1024//1024}MB)")
    except Exception as e:
        print("Art cache cleanup error: "+str(e))

AIRPORT_NAMES = {
    "ATL":"Atlanta","ORD":"OHare","LAX":"LAX","DFW":"Dallas","DEN":"Denver",
    "JFK":"JFK","LGA":"LaGuardia","EWR":"Newark","SFO":"SFO","SEA":"Seattle",
    "MIA":"Miami","CLT":"Charlotte","PHX":"Phoenix","IAH":"Houston","BOS":"Boston",
    "MSP":"MSP","DTW":"Detroit","PHL":"Philly","LAS":"Vegas","MCO":"Orlando",
    "BWI":"Baltimore","DCA":"Reagan","IAD":"Dulles","SAN":"SanDiego","TPA":"Tampa",
    "PDX":"Portland","SLC":"SaltLake","STL":"StLouis","MDW":"Midway","AUS":"Austin",
    "BNA":"Nashville","RDU":"Raleigh","PIT":"Pittsburgh","MEM":"Memphis","CVG":"Cincinnati",
    "IND":"Indy","MCI":"KansasCity","MSY":"NewOrleans","JAX":"Jax","FLL":"Lauderdale",
    "PBI":"WPalmBch","RSW":"FtMyers","SDF":"Louisville","ANC":"Anchorage","HNL":"Honolulu",
    "YYZ":"Toronto","YVR":"Vancouver","YUL":"Montreal","LHR":"London","CDG":"Paris",
    "AMS":"Amsterdam","FRA":"Frankfurt","NRT":"Tokyo","HND":"Haneda","DXB":"Dubai",
    "CHA":"Chattanooga","TYS":"Knoxville","HSV":"Huntsville","GSP":"Greenville",
}

AIRLINE_FULL_NAMES = {
    "DAL":"Delta","AAL":"American","UAL":"United","SWA":"Southwest","JBU":"JetBlue",
    "NKS":"Spirit","FFT":"Frontier","AAY":"Allegiant","ASA":"Alaska","HAL":"Hawaiian",
    "EDV":"Endeavor","JIA":"PSA","RPA":"Republic","SKW":"SkyWest","ENY":"Envoy",
    "FDX":"FedEx","UPS":"UPS","ACA":"AirCanada","BAW":"British","DLH":"Lufthansa",
}

AIRLINE_BRANDS = {
    "DAL":("DL",(255,255,255),(0,51,102)),"AAL":("AA",(255,255,255),(192,32,47)),
    "UAL":("UA",(255,255,255),(0,65,132)),"SWA":("WN",(255,191,0),(48,79,154)),
    "JBU":("B6",(255,255,255),(0,88,159)),"NKS":("NK",(255,234,0),(30,20,0)),
    "FFT":("F9",(255,255,255),(0,117,65)),"AAY":("G4",(255,255,255),(0,73,144)),
    "ASA":("AS",(255,255,255),(1,70,122)),"HAL":("HA",(255,255,255),(220,31,92)),
    "EDV":("9E",(255,255,255),(0,51,102)),"FDX":("FX",(255,128,0),(102,35,132)),
    "UPS":("5X",(255,191,0),(51,28,13)),"ACA":("AC",(255,255,255),(213,41,65)),
}

# ICAO (3-letter) → IATA (2-letter) for logo lookup
ICAO_TO_IATA = {
    "DAL":"DL","AAL":"AA","UAL":"UA","SWA":"WN","JBU":"B6","NKS":"NK","FFT":"F9",
    "AAY":"G4","ASA":"AS","HAL":"HA","EDV":"9E","JIA":"OH","RPA":"YX","SKW":"OO",
    "ENY":"MQ","FDX":"FX","UPS":"5X","ACA":"AC","BAW":"BA","DLH":"LH","AFR":"AF",
    "KLM":"KL","IBE":"IB","VRD":"VX","SXS":"XE","TRS":"3V","EIN":"EI","AIC":"AI",
    "ANZ":"NZ","QFA":"QF","SIA":"SQ","CPA":"CX","JAL":"JL","ANA":"NH","KAL":"KE",
    "CSN":"CZ","CCA":"CA","CHH":"HU","CES":"MU","HVN":"VN","THA":"TG","GIA":"GA",
    "MAS":"MH","PAL":"PR","TAM":"JJ","LAN":"LA","AZU":"AD","GOL":"G3","AVA":"AV",
    "AMX":"AM","ARG":"AR","UAE":"EK","ETD":"EY","QTR":"QR","SVA":"SV","THY":"TK",
    "MSR":"MS","TUN":"TU","RJA":"RJ","OMA":"WY","IAW":"IA","ELY":"LY","RAM":"AT",
    "EAL":"EA","TWA":"TW","PAA":"PA","CFG":"DE","EZY":"U2","RYR":"FR","WZZ":"W6",
    "VLG":"VY","IBK":"I2","BEL":"SN","LGL":"LG","CSA":"OK","LOT":"LO","MAL":"OV",
    "AUA":"OS","SAS":"SK","FIN":"AY","TAP":"TP","TRA":"HV","TOM":"BY","TCX":"MT",
    "WJA":"WS","CAL":"CI","EVA":"BR","FJI":"FJ","PIA":"PK","GBL":"GT","CHQ":"BW",
}

# NOTE: CLOCK_TIMEZONES is already imported from dashboard_config above.
# The duplicate definition that used to live here has been removed to avoid
# silent divergence if the config copy is ever edited.

WEATHER_LABELS = {
    0:"Clear",1:"Clear",2:"PtCloud",3:"Cloudy",45:"Fog",48:"Fog",
    51:"Drizzle",53:"Drizzle",55:"Drizzle",61:"Rain",63:"Rain",65:"HvyRain",
    71:"Snow",73:"Snow",75:"HvySnow",77:"Snow",80:"Shwr",81:"Shwr",82:"HvyRain",
    85:"Snow",86:"Snow",95:"Storm",96:"Storm",99:"Storm",
}

_override_cache = {}
_override_cache_time = 0.0

def get_override():
    global _override_cache, _override_cache_time
    if time.time() - _override_cache_time < 0.25:
        return _override_cache
    try:
        if os.path.exists(OVERRIDE_FILE):
            with open(OVERRIDE_FILE) as _ovf:
                _override_cache = json.loads(_ovf.read())
            _override_cache_time = time.time()
            return _override_cache
    except Exception:
        pass
    _override_cache_time = time.time()
    return _override_cache

def get_brightness():
    try:
        ov = get_override()
        if ov.get("brightness") is not None:
            return float(ov["brightness"])
        now = datetime.now(ZoneInfo(LOCATION_TZ))
        t = now.hour + now.minute / 60.0
        segs = get_bright_schedule().get("segments", [])
        if not segs:
            return 1.0
        for seg in segs:
            if t < seg.get("end", 24):
                return max(0.0, min(1.0, seg["bright"] / 100.0))
        return max(0.0, min(1.0, segs[-1]["bright"] / 100.0))
    except Exception:
        # Any parse/math error: default to full brightness so the display stays on
        return 1.0

def apply_dimming(img):
    b = get_brightness()
    if b >= 1.0: return img
    if b <= 0.0: return Image.new("RGB", img.size, (0,0,0))
    return ImageEnhance.Brightness(img).enhance(b)

def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3959
    dlat = math.radians(lat2-lat1); dlon = math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

_font_cache = {}
def get_font(size):
    if size in _font_cache: return _font_cache[size]
    paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for p in paths:
        if os.path.exists(p):
            f = ImageFont.truetype(p, size); _font_cache[size] = f; return f
    f = ImageFont.load_default(); _font_cache[size] = f; return f

def aqi_color(aqi):
    if aqi is None: return (150,150,150), "?"
    if aqi <= 50: return (0,228,0), "Good"
    if aqi <= 100: return (255,255,0), "Mod"
    if aqi <= 150: return (255,126,0), "USG"
    if aqi <= 200: return (255,0,0), "Bad"
    if aqi <= 300: return (143,63,151), "VBad"
    return (126,0,35), "Hzrd"

def uv_color(uv):
    if uv is None: return (150,150,150), "?"
    if uv < 3: return (0,228,0), "L"
    if uv < 6: return (255,255,0), "M"
    if uv < 8: return (255,126,0), "H"
    if uv < 11: return (255,0,0), "VH"
    return (143,63,151), "EX"

# ========== AIRLINE LOGO ==========
def get_airline_logo(icao, iata=None):
    """Fetch airline logo, trying high-quality CDNs first.
    Priority: Google gstatic (IATA) → avs.io (IATA) → GitHub (ICAO fallback)
    """
    if not icao: return None
    icao = icao.upper()
    cf   = LOGO_CACHE_DIR / (icao + ".png")
    miss = LOGO_CACHE_DIR / (icao + ".miss")
    if miss.exists(): return None
    if cf.exists():
        try:
            with Image.open(cf) as _img: return _img.copy()
        except Exception: pass  # corrupt cache file — fall through to re-download

    # Resolve IATA code: prefer caller-supplied, then static map
    if not iata:
        iata = ICAO_TO_IATA.get(icao)

    sources = []
    if iata:
        sources += [
            # Google Flights — best quality, near-complete airline coverage
            "https://www.gstatic.com/flights/airline_logos/70px/" + iata + ".png",
            # AVS.io — good 200×200 logos, solid secondary coverage
            "https://pics.avs.io/200/200/" + iata + ".png",
        ]
    # Original GitHub repo (ICAO-keyed) as last resort
    sources.append(
        "https://raw.githubusercontent.com/sexym0nk3y/airline-logos/main/logos/" + icao + ".png"
    )

    for url in sources:
        try:
            r = _session.get(url, timeout=4)
            if r.status_code != 200 or len(r.content) < 300:
                continue
            img = Image.open(BytesIO(r.content))
            # gstatic returns a tiny 717-byte blank L/LA placeholder for missing logos
            if img.mode in ("L", "LA") and len(r.content) < 1000:
                continue
            img.save(cf, "PNG")
            with Image.open(cf) as _img: return _img.copy()
        except Exception:
            pass

    miss.touch()
    return None

# ========== MUSIC CLIENTS ==========

# pyatv device model names that can play media (HomePods + Apple TVs)
_HOMEPOD_MODELS = {
    "HomePod", "HomePodMini", "HomePodGen2",
    "HomePodMiniGen1", "HomePod2ndGen",
    "AudioAccessory1,1", "AudioAccessory1,2",
    "AudioAccessory5,1", "AudioAccessory6,1",
}
_ATV_MODELS     = {"Gen2", "Gen3", "Gen4", "Gen4K",
                   "AppleTV4KGen2", "AppleTV4KGen3", "AppleTVGen1"}
_MEDIA_DEVICE_MODELS = _HOMEPOD_MODELS | _ATV_MODELS | {"Music"}

class HomePodManager:
    """Auto-discovers all HomePods and Apple TVs on the local network via mDNS.
    No static device IDs needed — just start it and it finds whatever is there."""

    RESCAN_INTERVAL = 30   # seconds between full network re-scans (was 60, lowered to pick up new devices faster)

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._conns = {}    # identifier -> live pyatv connection
        self._cfgs  = {}    # identifier -> pyatv config object (from scan)
        self._result = None
        self._result_time = 0.0
        self._lock   = threading.Lock()
        self._running = True
        self._last_scan = 0.0
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        self._loop.run_until_complete(self._poll_loop())

    async def _scan(self):
        """mDNS broadcast scan to discover HomePods and Apple TVs on the local network.

        This uses Zeroconf (mDNS) to find all compatible devices. The network must allow
        UDP traffic on port 5353 for this to work. If no devices are found, check:
        - Is mDNS/Bonjour enabled on your router?
        - Are your devices on the same network as the Pi?
        - Are there firewall rules blocking port 5353?
        """
        try:
            # Scan network for all Apple devices (15s timeout)
            found = await asyncio.wait_for(pyatv.scan(self._loop), timeout=15)
            if not found:
                print("  → No Apple devices found on network (mDNS scan returned empty)")
                self._last_scan = time.time()
                return

            print(f"  → mDNS scan found {len(found)} device(s)")
            for device in found:
                ident = device.identifier
                # Extract device model name (e.g., "HomePod", "HomePodMini", "AppleTV4KGen3")
                model_name = ""
                try:
                    if device.device_info and device.device_info.model:
                        model_name = device.device_info.model.name
                except Exception:
                    pass

                # Log every device found (even if we don't support it) so user can debug model mismatches
                print(f"     Device: {device.name!r} | Model: {model_name!r} | ID: {ident}")

                # Skip if we've already cached this device
                if ident in self._cfgs:
                    continue

                # Only add devices we recognize (HomePod/AppleTV models)
                if model_name not in _MEDIA_DEVICE_MODELS:
                    print(f"       ⚠ Model '{model_name}' not in supported list (won't use for music)")
                    continue

                # Device is supported — save it for polling
                self._cfgs[ident] = device
                dtype = "appletv" if model_name in _ATV_MODELS else "homepod"
                print(f"       ✓ Added {dtype}: {device.name}")
        except asyncio.TimeoutError:
            print(f"  → mDNS scan timed out after 15s (network may be slow or mDNS blocked)")
        except Exception as e:
            print(f"  → mDNS scan error: {e}")

        self._last_scan = time.time()

    def _dtype_for(self, ident):
        try:
            model_name = self._cfgs[ident].device_info.model.name
            return "appletv" if model_name in _ATV_MODELS else "homepod"
        except Exception:
            return "homepod"

    async def _get_conn(self, ident):
        if ident not in self._cfgs:
            return None
        if ident not in self._conns:
            try:
                cfg = self._cfgs[ident]
                name = cfg.name if hasattr(cfg, 'name') else ident
                self._conns[ident] = await asyncio.wait_for(
                    pyatv.connect(cfg, self._loop), timeout=5)
                print(f"[HomePod] Connected to {name}")
            except asyncio.TimeoutError:
                print(f"[HomePod] Connection timeout to {ident}")
                return None
            except Exception as e:
                print(f"[HomePod] Connect error {ident}: {type(e).__name__}: {str(e)[:80]}")
                return None
        return self._conns[ident]

    async def _poll_one(self, ident):
        try:
            conn = await self._get_conn(ident)
            if conn is None:
                return None
            info = await asyncio.wait_for(conn.metadata.playing(), timeout=3)
            state = str(info.device_state)
            if "Playing" not in state:
                # Not playing — this is normal, just return None silently
                return None
            if not info.title:
                return None
            dtype = self._dtype_for(ident)
            r = {"title": info.title, "artist": info.artist or "",
                 "artwork": None, "dtype": dtype}
            try:
                art = await asyncio.wait_for(conn.metadata.artwork(), timeout=3)
                if art and art.bytes:
                    r["artwork"] = art.bytes
            except Exception:
                pass
            return r
        except asyncio.TimeoutError:
            print(f"Device poll {ident}: timeout connecting/polling")
            self._conns.pop(ident, None)
            return None
        except Exception as e:
            print(f"Device poll {ident}: {type(e).__name__}: {e}")
            self._conns.pop(ident, None)
            return None

    async def _poll_loop(self):
        await self._scan()
        while self._running:
            try:
                # Periodically re-scan to pick up newly appeared devices
                if time.time() - self._last_scan > self.RESCAN_INTERVAL:
                    await self._scan()

                idents = list(self._cfgs.keys())
                if idents:
                    print(f"[HomePod] Polling {len(idents)} device(s)")
                    results = await asyncio.gather(
                        *[self._poll_one(i) for i in idents],
                        return_exceptions=True)
                    print(f"[HomePod] Poll results: {len([r for r in results if isinstance(r, dict) and r])} playing")
                else:
                    results = []
                playing = next((r for r in results if isinstance(r, dict) and r), None)
                if playing:
                    ap = None
                    if playing["artwork"]:
                        ah = hashlib.md5(playing["artwork"]).hexdigest()
                        af = CACHE_DIR / (playing["dtype"]+"_"+ah+".jpg")
                        if not af.exists():
                            try:
                                with Image.open(BytesIO(playing["artwork"])) as _art: _art.save(af, "JPEG")
                            except Exception: pass  # bad artwork bytes — skip art, still show track info
                        if af.exists(): ap = str(af)
                    final = {"track": playing["title"], "artist": playing["artist"],
                             "image_url": ap, "source": playing["dtype"], "is_local_file": True}
                else:
                    final = None
                with self._lock:
                    self._result = final
                    self._result_time = time.time()
            except Exception as e:
                print(f"[HomePod] Poll loop error: {type(e).__name__}: {e}")
                with self._lock:
                    self._result = None
                    self._result_time = time.time()
            await asyncio.sleep(HOMEPOD_POLL_INTERVAL)

    def get_playing(self):
        with self._lock:
            if self._result and (time.time() - self._result_time) > (HOMEPOD_POLL_INTERVAL * 3):
                return None
            return self._result

class SpotifyClient:
    def __init__(self, cid, csec, rtok):
        self.cid = cid; self.csec = csec; self.rtok = rtok
        self.access_token = None; self.token_expiry = 0
        self.backoff_until = 0

    def refresh_access_token(self):
        auth = base64.b64encode((self.cid+":"+self.csec).encode()).decode()
        try:
            # Use the shared pooled session (reuses the TLS connection to
            # accounts.spotify.com) instead of a one-off requests.post.
            r = _session.post("https://accounts.spotify.com/api/token",
                headers={"Authorization":"Basic "+auth,"Content-Type":"application/x-www-form-urlencoded"},
                data={"grant_type":"refresh_token","refresh_token":self.rtok}, timeout=5)
            r.raise_for_status(); d = r.json()
            self.access_token = d["access_token"]
            self.token_expiry = time.time() + d.get("expires_in", 3600)
            return True
        except Exception as e:
            print("Spotify token error: "+str(e)); return False

    def get_currently_playing(self):
        """Return the currently playing track dict, or None if nothing is playing.
        Never returns stale data — any error or stopped state yields None so the
        display clears promptly."""
        if time.time() < self.backoff_until:
            return None
        if time.time() >= self.token_expiry:
            if not self.refresh_access_token():
                return None   # token refresh failed — clear display rather than freeze
        try:
            r = _session.get("https://api.spotify.com/v1/me/player/currently-playing",
                headers={"Authorization":"Bearer "+self.access_token}, timeout=4)
            if r.status_code == 429:
                retry_after = min(int(r.headers.get("Retry-After", "30")), 300)
                self.backoff_until = time.time() + retry_after
                print("Spotify 429: backing off " + str(retry_after) + "s")
                return None
            if r.status_code == 204:   # nothing playing / player inactive
                return None
            r.raise_for_status()
            data = r.json()
            if not data.get("is_playing", False):   # paused or stopped
                return None
            item = data.get("item")
            if not item:                             # ad / transition / podcast
                return None
            images = item.get("album",{}).get("images",[])
            iu = images[0]["url"] if images else None
            artists = item.get("artists",[])
            return {"track":  item.get("name","?"),
                    "artist": artists[0]["name"] if artists else "?",
                    "image_url": iu, "source": "spotify"}
        except Exception as e:
            print("Spotify error: "+str(e))
            return None   # clear display on any network/parse error

# ========== WEATHER + AQI + SUN ==========
class WeatherClient:
    # wttr.in code → WMO code used by draw_small_weather_icon
    _WTTR_TO_WMO = {
        113:0, 116:2, 119:3, 122:3, 143:45, 248:45, 260:48,
        176:61, 293:61, 296:61, 263:51, 266:53, 281:55, 284:55, 185:55,
        299:63, 302:63, 305:65, 308:65, 353:80, 356:81, 359:82,
        179:71, 323:71, 326:71, 317:71, 320:73, 329:73, 332:73,
        335:75, 338:75, 227:75, 230:75, 182:77, 311:77, 314:77,
        350:77, 374:77, 377:77, 362:85, 368:85, 365:86, 371:86,
        200:95, 386:95, 389:96, 392:96, 395:99,
    }
    def __init__(self):
        self.cache = None; self.cache_time = 0; self.retry_after = 0
        self._load_disk()
    def _disk_path(self):
        return CACHE_DIR / "weather_data.json"
    def _load_disk(self):
        try:
            p = self._disk_path()
            if p.exists():
                d = json.loads(p.read_text())
                self.cache = d.get("data")
                self.cache_time = d.get("time", 0)
                print("Weather: loaded from disk cache")
        except Exception: pass
    def _save_disk(self):
        try:
            self._disk_path().write_text(json.dumps({"data": self.cache, "time": self.cache_time}))
        except Exception: pass
    def _parse_wttr(self, j):
        """Parse wttr.in JSON into the same dict shape as open-meteo."""
        day_names = ["MON","TUE","WED","THU","FRI","SAT","SUN"]
        cur = j["current_condition"][0]
        days = []
        for i, day in enumerate(j.get("weather", [])[:3]):
            try: lbl = "Now" if i == 0 else day_names[datetime.fromisoformat(day["date"]).weekday()]
            except Exception: lbl = "?"
            # midday hourly slot (index 4 = 12:00) for the day icon code
            hr = (day.get("hourly") or [{}]*5)[4]
            raw_code = self._WTTR_TO_WMO.get(int(hr.get("weatherCode", 113)), 0)
            # Same precip-chance + cloud-cover sanity check as open-meteo.
            try: pp = float(hr.get("chanceofrain", 0))
            except Exception: pp = 0
            try: cc = float(hr.get("cloudcover", 0))
            except Exception: cc = 0
            days.append({"label": lbl,
                         "high": float(day["maxtempF"]),
                         "low":  float(day["mintempF"]),
                         "code": self._derive_code(raw_code, pp, cc)})
        # expand 8 three-hour UV samples → 24 hourly by repeating each 3×
        hourly_uv = []
        for h in (j.get("weather") or [{}])[0].get("hourly", []):
            val = float(h.get("uvIndex", 0))
            hourly_uv += [val, val, val]
        hourly_uv = (hourly_uv + [0]*24)[:24]
        raw_cur_code = self._WTTR_TO_WMO.get(int(cur.get("weatherCode", 113)), 0)
        try: cur_cloud = float(cur.get("cloudcover", 0))
        except Exception: cur_cloud = 0
        try: cur_precip_in = float(cur.get("precipInches", 0))
        except Exception: cur_precip_in = 0
        # If it's actively precipitating now, treat as 100% chance; else 0 and let
        # cloud cover decide (mirrors the open-meteo derivation).
        cur_code = self._derive_code(raw_cur_code, 100 if cur_precip_in > 0.01 else 0, cur_cloud)
        return {
            "current_temp":       float(cur["temp_F"]),
            "current_feels":      float(cur["FeelsLikeF"]),
            "current_code":       cur_code,
            "current_uv":         float(cur.get("uvIndex", 0)),
            "current_humidity":   float(cur.get("humidity", 0)),
            "current_wind_speed": float(cur.get("windspeedMiles", 0)),
            "current_wind_dir":   float(cur.get("winddirDegree", 0)),
            "current_precip":     float(cur.get("precipInches", 0)),
            "days": days,
            "hourly_uv": hourly_uv,
        }
    @staticmethod
    def _derive_code(raw, precip_prob, cloud):
        """Pick a sensible icon code. open-meteo's weather_code badly over-reports
        thunderstorms on dry days (e.g. code 95 with 1% precip chance and 9%
        cloud), so we trust the actual precipitation probability and cloud cover
        instead. Returns a WMO code understood by draw_small_weather_icon."""
        raw = raw or 0
        pp = precip_prob if precip_prob is not None else 0
        cc = cloud if cloud is not None else 0
        # Only show wet weather when there's a real chance of precip.
        if pp >= 40 and raw >= 45:
            # A "slight chance" storm shouldn't render as a full thunderstorm.
            if raw in (95, 96, 99) and pp < 60:
                return 63          # show rain instead
            return raw
        # Dry: choose clear / partly-cloudy / cloudy purely from cloud cover.
        if cc < 25: return 0       # clear
        if cc < 60: return 2       # partly cloudy
        return 3                   # cloudy

    def fetch(self):
        now = time.time()
        if self.cache and now - self.cache_time < WEATHER_POLL_INTERVAL:
            return self.cache
        if now < self.retry_after:
            return self.cache
        # ── Primary: open-meteo ──────────────────────────────────────────
        try:
            url = ("https://api.open-meteo.com/v1/forecast?latitude="+str(CHATTANOOGA_LAT)+
                   "&longitude="+str(CHATTANOOGA_LON)+
                   "&current=temperature_2m,apparent_temperature,weather_code,uv_index,relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation,cloud_cover"+
                   "&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,cloud_cover_mean"+
                   "&hourly=uv_index"+
                   "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone="+LOCATION_TZ+"&forecast_days=3")
            r = _session.get(url, timeout=5); r.raise_for_status()
            if r.json().get("error"): raise Exception(r.json().get("reason","open-meteo error"))
            j = r.json()
            cur = j.get("current",{}); daily = j.get("daily",{}); hourly = j.get("hourly",{})
            day_names = ["MON","TUE","WED","THU","FRI","SAT","SUN"]
            ta = daily.get("time",[]); ha = daily.get("temperature_2m_max",[])
            la = daily.get("temperature_2m_min",[]); ca = daily.get("weather_code",[])
            pa = daily.get("precipitation_probability_max",[])   # % chance of rain
            cca = daily.get("cloud_cover_mean",[])               # mean cloud cover %
            days = []
            for i in range(min(3, len(ta))):
                try:
                    dt = datetime.fromisoformat(ta[i])
                    lbl = "Now" if i == 0 else day_names[dt.weekday()]
                except Exception: lbl = "?"
                # Derive the icon from real precip chance + cloud cover, not the
                # unreliable raw weather_code (which over-reports storms).
                code = self._derive_code(ca[i] if i < len(ca) else None,
                                         pa[i] if i < len(pa) else None,
                                         cca[i] if i < len(cca) else None)
                days.append({"label":lbl,"high":ha[i] if i<len(ha) else None,
                            "low":la[i] if i<len(la) else None,
                            "code":code})
            # Current conditions: use today's precip chance + the current cloud cover.
            cur_code = self._derive_code(cur.get("weather_code"),
                                         pa[0] if pa else None,
                                         cur.get("cloud_cover"))
            self.cache = {"current_temp":cur.get("temperature_2m"),
                         "current_feels":cur.get("apparent_temperature"),
                         "current_code":cur_code,
                         "current_uv":cur.get("uv_index"),
                         "current_humidity":cur.get("relative_humidity_2m"),
                         "current_wind_speed":cur.get("wind_speed_10m"),
                         "current_wind_dir":cur.get("wind_direction_10m"),
                         "current_precip":cur.get("precipitation"),
                         "days":days,
                         "hourly_uv": hourly.get("uv_index",[])[:24]}
            self.cache_time = time.time(); self.retry_after = 0
            self._save_disk()
            print("Weather (open-meteo): "+str(int(self.cache["current_temp"]))+"F")
            return self.cache
        except Exception as e:
            print("Weather open-meteo error: "+str(e))
        # ── Fallback: wttr.in ────────────────────────────────────────────
        try:
            r = _session.get(
                "https://wttr.in/"+LOCATION_NAME.replace(" ","+")+"?format=j1",
                timeout=8)
            r.raise_for_status()
            self.cache = self._parse_wttr(r.json())
            self.cache_time = time.time(); self.retry_after = 0
            self._save_disk()
            print("Weather (wttr.in): "+str(int(self.cache["current_temp"]))+"F")
            return self.cache
        except Exception as e:
            print("Weather wttr.in error: "+str(e))
            self.retry_after = time.time() + 120
            return self.cache

class AirQualityClient:
    def __init__(self):
        self.cache = None; self.cache_time = 0; self.retry_after = 0
        self._load_disk()
    def _disk_path(self):
        return CACHE_DIR / "aqi_data.json"
    def _load_disk(self):
        try:
            p = self._disk_path()
            if p.exists():
                d = json.loads(p.read_text())
                self.cache = d.get("data")
                self.cache_time = d.get("time", 0)
                print("AQI: loaded from disk cache")
        except Exception: pass
    def _save_disk(self):
        try:
            self._disk_path().write_text(json.dumps({"data": self.cache, "time": self.cache_time}))
        except Exception: pass
    def fetch(self):
        now = time.time()
        if self.cache and now - self.cache_time < WEATHER_POLL_INTERVAL:
            return self.cache
        if now < self.retry_after:
            return self.cache
        try:
            url = ("https://air-quality-api.open-meteo.com/v1/air-quality?latitude="+
                   str(CHATTANOOGA_LAT)+"&longitude="+str(CHATTANOOGA_LON)+
                   "&current=us_aqi,pm2_5,pm10")
            r = _session.get(url, timeout=5); r.raise_for_status()
            cur = r.json().get("current",{})
            self.cache = {"aqi":cur.get("us_aqi"),"pm25":cur.get("pm2_5"),"pm10":cur.get("pm10")}
            self.cache_time = time.time()
            self.retry_after = 0
            self._save_disk()
            print("AQI: "+str(self.cache["aqi"]))
            return self.cache
        except Exception as e:
            print("AQI error: "+str(e))
            self.retry_after = time.time() + 120  # wait 2 min before retry
            return self.cache

class SunClient:
    def __init__(self):
        self.cache = None; self.cache_date = None
        self._load_disk()
    def _disk_path(self):
        return CACHE_DIR / "sun_data.json"
    def _load_disk(self):
        try:
            p = self._disk_path()
            if p.exists():
                d = json.loads(p.read_text())
                sr = d.get("sunrise"); ss = d.get("sunset"); dt = d.get("date")
                if sr and ss and dt:
                    self.cache = {"sunrise": datetime.fromisoformat(sr),
                                  "sunset":  datetime.fromisoformat(ss)}
                    self.cache_date = datetime.fromisoformat(dt).date()
                    print("Sun: loaded from disk cache")
        except Exception: pass
    def _save_disk(self):
        try:
            self._disk_path().write_text(json.dumps({
                "sunrise": self.cache["sunrise"].isoformat(),
                "sunset":  self.cache["sunset"].isoformat(),
                "date":    self.cache_date.isoformat(),
            }))
        except Exception: pass
    def fetch(self):
        today = datetime.now(ZoneInfo(LOCATION_TZ)).date()
        if self.cache and self.cache_date == today: return self.cache
        # ── Primary: open-meteo ──────────────────────────────────────────
        try:
            url = ("https://api.open-meteo.com/v1/forecast?latitude="+str(CHATTANOOGA_LAT)+
                   "&longitude="+str(CHATTANOOGA_LON)+
                   "&daily=sunrise,sunset&timezone="+LOCATION_TZ+"&forecast_days=1")
            r = _session.get(url, timeout=5); r.raise_for_status()
            if r.json().get("error"): raise Exception(r.json().get("reason","open-meteo error"))
            d = r.json().get("daily",{})
            srs = d.get("sunrise") or []; sss = d.get("sunset") or []
            if not srs or not sss: raise Exception("empty sunrise/sunset")
            sr = datetime.fromisoformat(srs[0]); ss = datetime.fromisoformat(sss[0])
            self.cache = {"sunrise":sr,"sunset":ss}
            self.cache_date = today
            self._save_disk()
            print("Sun (open-meteo): "+sr.strftime("%H:%M")+" - "+ss.strftime("%H:%M"))
            return self.cache
        except Exception as e:
            print("Sun open-meteo error: "+str(e))
        # ── Fallback: wttr.in ────────────────────────────────────────────
        try:
            r = _session.get(
                "https://wttr.in/"+LOCATION_NAME.replace(" ","+")+"?format=j1",
                timeout=8)
            r.raise_for_status()
            astro = r.json()["weather"][0]["astronomy"][0]
            tz = ZoneInfo(LOCATION_TZ)
            def _parse_wttr_time(s):
                # e.g. "06:27 AM" or "8:52 PM"
                return datetime.strptime(s.strip(), "%I:%M %p").replace(
                    year=today.year, month=today.month, day=today.day,
                    tzinfo=tz)
            sr = _parse_wttr_time(astro["sunrise"])
            ss = _parse_wttr_time(astro["sunset"])
            self.cache = {"sunrise":sr,"sunset":ss}
            self.cache_date = today
            self._save_disk()
            print("Sun (wttr.in): "+sr.strftime("%H:%M")+" - "+ss.strftime("%H:%M"))
            return self.cache
        except Exception as e:
            print("Sun wttr.in error: "+str(e))
            return self.cache

# ========== ISS ==========
class ISSTracker:
    def __init__(self):
        self.lat = None; self.lon = None
        self.distance = None; self.last_distance = None
        self.last_poll_time = 0
        self.retry_after = 0
    def poll(self):
        if time.time() < self.retry_after:
            return False
        try:
            r = _session.get("http://api.open-notify.org/iss-now.json", timeout=5)
            r.raise_for_status()
            pos = r.json().get("iss_position",{})
            self.lat = float(pos.get("latitude")); self.lon = float(pos.get("longitude"))
            self.last_distance = self.distance
            self.distance = haversine_miles(CHATTANOOGA_LAT, CHATTANOOGA_LON, self.lat, self.lon)
            self.last_poll_time = time.time()
            self.retry_after = 0
            return True
        except Exception as e:
            print("ISS error: "+str(e))
            self.retry_after = time.time() + 120
            return False
    def is_fresh(self):
        return time.time() - self.last_poll_time < 120  # stale after 2 min
    def just_became_overhead(self):
        if self.distance is None: return False
        if self.last_distance is None: return self.distance < ISS_OVERHEAD_RADIUS_MILES
        return self.distance < ISS_OVERHEAD_RADIUS_MILES and self.last_distance >= ISS_OVERHEAD_RADIUS_MILES

# ========== FLIGHTS ==========
class FlightTracker:
    """
    Polls 3 ADS-B APIs (adsb.lol, airplanes.live, adsb.fi) in background thread.
    - Rotates between APIs so no single API is hammered
    - Tries up to 3 times if one API fails
    - Caches route info (origin/dest) to avoid repeated API calls
    - Removes stale aircraft after 90 seconds of no updates
    - Thread-safe with lock for position updates
    """
    def __init__(self):
        self.route_cache = {}           # Cached flight routes: {callsign: {origin, dest, airline_name}}
        self.positions = {}             # Current aircraft: {callsign: {lat, lon, altitude, speed, heading, ...}}
        self.lock = threading.Lock()    # Protects positions dict from race conditions
        self.api_index = 0              # Which API to try first (rotates)
        self._route_pending = set()     # Callsigns currently being fetched in background
                                        # NOTE: accessed from main + route threads without a lock.
                                        # The GIL makes individual add/discard atomic, so the
                                        # worst case of a check-then-add race is a duplicate
                                        # route fetch (harmless — result is idempotent).

    def poll_in_background(self):
        """
        Non-blocking: runs in daemon thread via start_poll().
        Queries ADS-B APIs in rotation, parses results, caches, cleans stale data.
        """
        apis = [self._poll_adsblol, self._poll_airplaneslive, self._poll_adsbfi]
        planes = []
        tries = 0
        # Try APIs in rotation until we get results or exhaust all 3
        while not planes and tries < len(apis):
            idx = (self.api_index + tries) % len(apis)
            planes = apis[idx]()
            tries += 1
        # Rotate which API we try first next time (load balance)
        self.api_index = (self.api_index + 1) % len(apis)
        now = time.time()
        with self.lock:
            for p in planes:
                self.positions[p["callsign"]] = {
                    "lat":p["lat"],"lon":p["lon"],"altitude_ft":p["altitude_ft"],
                    "speed_mph":p["speed_mph"],"heading":p["heading"],
                    "distance":p["distance"],"type":p.get("type",""),
                    "registration":p.get("registration",""),
                    "category":p.get("category",""),"timestamp":now}
            stale = [k for k,v in self.positions.items() if now - v["timestamp"] > 90]
            for k in stale: del self.positions[k]
    def _poll_adsblol(self):
        try:
            r = _session.get("https://api.adsb.lol/v2/lat/"+str(CHATTANOOGA_LAT)+"/lon/"+str(CHATTANOOGA_LON)+"/dist/"+str(RADIUS_MILES), timeout=8)
            r.raise_for_status()
            return self._parse(r.json().get("ac") or [])
        except Exception as e:
            print("adsb.lol err: "+str(e)); return []
    def _poll_airplaneslive(self):
        try:
            r = _session.get("https://api.airplanes.live/v2/point/"+str(CHATTANOOGA_LAT)+"/"+str(CHATTANOOGA_LON)+"/"+str(RADIUS_MILES), timeout=8)
            r.raise_for_status()
            return self._parse(r.json().get("ac") or [])
        except Exception as e:
            print("airplanes.live err: "+str(e)); return []
    def _poll_adsbfi(self):
        # adsb.fi retired api.adsb.fi/v2/... (now 404) and moved to
        # opendata.adsb.fi, which returns the aircraft list under "aircraft"
        # instead of "ac" (the per-aircraft fields are otherwise identical).
        try:
            r = _session.get("https://opendata.adsb.fi/api/v2/lat/"+str(CHATTANOOGA_LAT)+"/lon/"+str(CHATTANOOGA_LON)+"/dist/"+str(RADIUS_MILES), timeout=8)
            r.raise_for_status()
            data = r.json()
            return self._parse(data.get("aircraft") or data.get("ac") or [])
        except Exception as e:
            print("adsb.fi err: "+str(e)); return []
    def _parse(self, ac_list):
        seen = {}  # callsign → best (closest) entry; deduplicates within one API response
        for ac in ac_list:
            cs = (ac.get("flight") or "").strip()
            lat = ac.get("lat"); lon = ac.get("lon")
            alt = ac.get("alt_baro"); gs = ac.get("gs"); track = ac.get("track")
            if not cs or lat is None or lon is None: continue
            if isinstance(alt, str): alt = 0          # "ground" strings → 0
            alt_ft = int(alt) if alt else 0
            if alt_ft < 200: continue                 # skip ground / taxiing traffic
            d = haversine_miles(CHATTANOOGA_LAT, CHATTANOOGA_LON, lat, lon)
            if d > RADIUS_MILES: continue
            cat = (ac.get("category") or "").strip()
            entry = {"callsign":cs,"lat":lat,"lon":lon,
                "altitude_ft":alt_ft,
                "speed_mph":int(gs*1.151) if gs else 0,
                "heading":track if track is not None else 0,
                "distance":d,"type":(ac.get("t") or "").strip(),
                "registration":(ac.get("r") or "").strip(),
                "category":cat}
            # Keep only the closest entry per callsign
            if cs not in seen or d < seen[cs]["distance"]:
                seen[cs] = entry
        result = sorted(seen.values(), key=lambda p: p["distance"])
        return result
    def start_poll(self):
        threading.Thread(target=self.poll_in_background, daemon=True).start()
    def get_interpolated_planes(self):
        now = time.time(); result = []
        with self.lock:
            for cs, d in self.positions.items():
                dt = now - d["timestamp"]
                if dt > 90: continue
                # Dead-reckon the position forward from the last fix so the card
                # shows a smooth, up-to-date distance between polls.
                #   1° latitude  ≈ 69 miles everywhere
                #   1° longitude ≈ 69·cos(latitude) miles (meridians converge)
                spd = (d["speed_mph"]/3600)/69.0          # degrees of latitude per second
                hr  = math.radians(d["heading"])
                coslat = math.cos(math.radians(d["lat"])) or 1e-6   # guard against /0 at the poles
                nlat = d["lat"] + spd*dt*math.cos(hr)
                nlon = d["lon"] + spd*dt*math.sin(hr)/coslat
                nd = haversine_miles(CHATTANOOGA_LAT, CHATTANOOGA_LON, nlat, nlon)
                if nd <= RADIUS_MILES:
                    result.append({"callsign":cs,"lat":nlat,"lon":nlon,
                        "altitude_ft":d["altitude_ft"],"speed_mph":d["speed_mph"],
                        "heading":d["heading"],"distance":nd,"type":d.get("type",""),
                        "registration":d.get("registration",""),
                        "category":d.get("category","")})
        result.sort(key=lambda p: p["distance"])
        return result
    def get_route_info(self, callsign):
        c = self.route_cache.get(callsign)
        if c and time.time() < c["expiry"]: return c
        result = {"origin":None,"dest":None,"airline_icao":None,"airline_iata":None,"airline_name":None,
                  "origin_city":None,"dest_city":None,
                  "origin_lat":None,"origin_lon":None,
                  "dest_lat":None,"dest_lon":None,
                  "expiry":time.time()+3600}
        # ── Primary: adsbdb.com ──────────────────────────────────────────
        try:
            r = _session.get("https://api.adsbdb.com/v0/callsign/"+callsign, timeout=3)
            if r.status_code == 200:
                route = r.json().get("response",{}).get("flightroute",{})
                org = route.get("origin") or {}
                dst = route.get("destination") or {}
                result["origin"] = org.get("iata_code")
                result["dest"] = dst.get("iata_code")
                result["origin_city"] = org.get("municipality")
                result["dest_city"] = dst.get("municipality")
                result["origin_lat"] = org.get("latitude")
                result["origin_lon"] = org.get("longitude")
                result["dest_lat"] = dst.get("latitude")
                result["dest_lon"] = dst.get("longitude")
                airline = route.get("airline") or {}
                result["airline_icao"] = airline.get("icao")
                result["airline_iata"] = airline.get("iata")
                result["airline_name"] = airline.get("name")
                result["expiry"] = time.time() + 43200
        except Exception as e:
            print("adsbdb err: "+str(e))
        # ── Fallback: OpenSky Network routes (catches charters, medevac, military) ──
        if not result["origin"] and not result["dest"]:
            try:
                r2 = _session.get("https://opensky-network.org/api/routes",
                                  params={"callsign": callsign}, timeout=4)
                if r2.status_code == 200:
                    data = r2.json()
                    apts = data.get("route") or []
                    if len(apts) >= 1: result["origin"] = apts[0]
                    if len(apts) >= 2: result["dest"]   = apts[-1]
                    if result["origin"] or result["dest"]:
                        result["expiry"] = time.time() + 43200
                        print(f"OpenSky route hit for {callsign}: {apts}")
            except Exception as e:
                print("opensky route err: "+str(e))
        self.route_cache[callsign] = result
        self._route_pending.discard(callsign)
        # Prune cache when it gets large — remove expired entries first.
        # Snapshot items() first: several route threads + the main loop touch
        # route_cache concurrently, so iterating it live can raise
        # "dictionary changed size during iteration".
        if len(self.route_cache) > 400:
            _now = time.time()
            expired = [k for k, v in list(self.route_cache.items()) if _now > v.get("expiry", 0)]
            for k in expired[:200]:
                self.route_cache.pop(k, None)   # pop() tolerates a key another thread already removed
        # Pre-warm airline logo cache so render_flight_image never blocks
        ai   = result.get("airline_icao") or (callsign[:3].upper() if len(callsign) >= 3 and callsign[:3].isalpha() else None)
        iata = result.get("airline_iata")
        if ai:
            get_airline_logo(ai, iata)
        return result

    def fetch_by_callsign(self, callsign):
        """Fetch a specific callsign globally (not radius-limited). Returns plane dict or None."""
        for url in [
            "https://api.adsb.lol/v2/callsign/"+callsign,
            "https://api.airplanes.live/v2/callsign/"+callsign,
            "https://opendata.adsb.fi/api/v2/callsign/"+callsign,   # moved from api.adsb.fi
        ]:
            try:
                r = _session.get(url, timeout=5)
                if r.status_code != 200: continue
                data = r.json()
                ac_list = data.get("ac") or data.get("aircraft") or []  # opendata.adsb.fi uses "aircraft"
                if not ac_list: continue
                ac = ac_list[0]
                lat = ac.get("lat"); lon = ac.get("lon")
                alt = ac.get("alt_baro"); gs = ac.get("gs"); track = ac.get("track")
                if lat is None or lon is None: continue
                if isinstance(alt, str): alt = 0
                d = haversine_miles(CHATTANOOGA_LAT, CHATTANOOGA_LON, lat, lon)
                return {"callsign": callsign,
                        "lat": lat, "lon": lon,
                        "altitude_ft": int(alt) if alt else 0,
                        "speed_mph": int(gs * 1.151) if gs else 0,
                        "heading": track if track is not None else 0,
                        "distance": d,
                        "type": (ac.get("t") or "").strip(),
                        "registration": (ac.get("r") or "").strip()}
            except Exception as e:
                print("fetch_by_callsign err: "+str(e))
        return None

# ========== ICONS ==========
# ICAO type codes that are rotorcraft (helicopters / gyrocopters)
_HELI_TYPES = {
    # Bell
    "B06","B07","B37","B47","B412","B407","B427","B429","B430","B505","B2T",
    # Airbus/Eurocopter
    "EC35","EC45","EC55","EC75","EC25","EC20","H125","H130","H135","H145","H155","H160","H175","H215","H225",
    "AS32","AS50","AS55","AS65","AS35","AS45","AS15","AS02",
    # Sikorsky
    "S76","S92","S300","S333","S58T","S61","S62","S64","S65","S69","S70",
    # Robinson
    "R22","R44","R66",
    # AgustaWestland / Leonardo
    "A109","A119","A129","A139","A169","A189","AW09","AW19","AW39","AW89",
    # MD Helicopters
    "MD52","MD53","MD55","MD58",
    # MIL/Kamov
    "K26D","MI8","MI17",
    # Generic H-prefixed military/utility
    "H1","H46","H47","H53","H60","H64","H72","H76","H1T","H1Z","H60H","H60U",
    # Others
    "BO08","BO05","EC13","EC20","NH90","CH47","UH1Y","UH72",
}

def _is_helicopter(plane):
    """Return True if the plane is a rotorcraft based on ADS-B category or type code."""
    cat = (plane.get("category") or "").upper()
    if cat.startswith("B") and cat[1:].isdigit():
        return True  # ADS-B category B1–B4 = rotorcraft
    t = (plane.get("type") or "").upper().strip()
    if t in _HELI_TYPES:
        return True
    # Many military/medivac helicopters come through with H-prefixed ICAO designators
    if len(t) >= 2 and t[0] == "H" and t[1:3].isdigit():
        return True
    return False

def draw_heading_arrow(draw, cx, cy, radius, heading_deg, color=(255,200,0)):
    rel = (heading_deg - USER_FACING_DEG) % 360
    a = math.radians(rel)
    tx = int(round(cx + radius*math.sin(a))); ty = int(round(cy - radius*math.cos(a)))
    al = math.radians(rel + 140); ar = math.radians(rel - 140)
    lx = int(round(cx + (radius*0.7)*math.sin(al))); ly = int(round(cy - (radius*0.7)*math.cos(al)))
    rx = int(round(cx + (radius*0.7)*math.sin(ar))); ry = int(round(cy - (radius*0.7)*math.cos(ar)))
    pts = [(tx,ty),(lx,ly),(cx,cy),(rx,ry)]
    draw.polygon(pts, fill=color, outline=color)

def draw_small_weather_icon(draw, cx, cy, code, frame=0):
    if code is None: code = 0
    if code in [0,1]:
        # ☀️ Sunny — bright yellow sun + fixed rays
        draw.ellipse([(cx-4,cy-4),(cx+4,cy+4)], fill=(255,220,0))
        for a in range(0, 360, 45):
            rad = math.radians(a)
            x1 = cx + int(round(5.5*math.cos(rad))); y1 = cy + int(round(5.5*math.sin(rad)))
            x2 = cx + int(round(8.0*math.cos(rad))); y2 = cy + int(round(8.0*math.sin(rad)))
            draw.line([(x1,y1),(x2,y2)], fill=(255,180,0), width=1)
    elif code == 2:
        # ⛅ Partly cloudy — sun peeking behind cloud
        draw.ellipse([(cx-6,cy-5),(cx-1,cy)], fill=(255,210,0))
        draw.ellipse([(cx-6,cy-2),(cx+2,cy+3)], fill=(190,190,210))
        draw.ellipse([(cx-2,cy-4),(cx+6,cy+2)], fill=(210,210,225))
    elif code in [3,45,48]:
        # ☁️ Cloudy — animated bobbing cloud
        bob = 1 if (frame // 2) % 2 else 0
        draw.ellipse([(cx-7,cy-1+bob),(cx+7,cy+4+bob)], fill=(155,155,175))
        draw.ellipse([(cx-5,cy-4+bob),(cx+0,cy-1+bob)], fill=(175,175,195))
        draw.ellipse([(cx-1,cy-5+bob),(cx+5,cy-1+bob)], fill=(185,185,200))
        draw.ellipse([(cx+2,cy-3+bob),(cx+7,cy+0+bob)], fill=(170,170,190))
    elif code in [51,53,55,61,63,65,80,81,82]:
        # 🌧️ Rain — gray cloud + animated falling drops
        draw.ellipse([(cx-6,cy-3),(cx+6,cy+1)], fill=(120,120,145))
        draw.ellipse([(cx-4,cy-5),(cx+2,cy-2)], fill=(140,140,165))
        drop_off = (frame * 2) % 6
        for i,dx in enumerate([-3,0,3]):
            y0 = cy+2 + (drop_off + i*2) % 6
            if y0 <= cy+8:
                draw.line([(cx+dx,y0),(cx+dx-1,min(y0+2,cy+8))], fill=(70,140,255), width=1)
    elif code in [71,73,75,77,85,86]:
        # ❄️ Snow — light cloud + drifting white flakes
        draw.ellipse([(cx-6,cy-3),(cx+6,cy+1)], fill=(175,175,200))
        draw.ellipse([(cx-4,cy-5),(cx+2,cy-2)], fill=(195,195,215))
        for i,dx in enumerate([-3,0,3]):
            yf = cy+3 + (frame + i) % 4
            draw.point((cx+dx, min(yf,cy+7)), fill=(230,240,255))
            draw.point((cx+dx+1, min(yf+1,cy+7)), fill=(210,225,255))
    elif code in [95,96,99]:
        # ⛈️ Storm — dark cloud + flashing lightning bolt
        draw.ellipse([(cx-7,cy-2),(cx+7,cy+2)], fill=(60,60,85))
        draw.ellipse([(cx-5,cy-5),(cx+0,cy-1)], fill=(75,75,100))
        draw.ellipse([(cx-1,cy-5),(cx+5,cy-1)], fill=(70,70,95))
        f4 = frame % 4
        if f4 == 1:   bolt = (255,245,60)
        elif f4 == 2: bolt = (200,185,20)
        else:          bolt = None
        if bolt:
            draw.polygon([(cx,cy+2),(cx+3,cy+2),(cx,cy+6),(cx+2,cy+6),(cx-1,cy+10)], fill=bolt)
    else:
        draw.ellipse([(cx-4,cy-4),(cx+4,cy+4)], fill=(255,220,0))

# ========== RENDERERS ==========
# ── Clock settings (web-toggleable) ──────────────────────────────────────
# Default zones come from the config CLOCK_TIMEZONES (label, tz, color) tuples.
_DEFAULT_CLOCK_ZONES = [(l, tz) for (l, tz, _c) in CLOCK_TIMEZONES][:4]
# Fixed per-slot colours for the 4-zone grid (slot order = top-left, top-right,
# bottom-left, bottom-right). Keeps the look stable regardless of which zones
# the user picks on the web page.
_CLOCK_SLOT_COLORS = [(255,255,100), (200,255,200), (180,220,255), (255,150,200)]
SINGLE_CLOCK_TZ = "America/New_York"   # Eastern, used by the single-zone mode

_clock_settings_cache = None
_clock_settings_cache_time = 0.0
def _load_clock_settings():
    """Returns {"mode": "single"|"quad", "zones": [(label, tz), ...]}.
    Cached 2s so the per-second clock render doesn't re-read disk every frame."""
    global _clock_settings_cache, _clock_settings_cache_time
    if _clock_settings_cache is not None and time.time() - _clock_settings_cache_time < 2.0:
        return _clock_settings_cache
    result = {"mode": "single", "zones": list(_DEFAULT_CLOCK_ZONES)}
    try:
        if os.path.exists(CLOCK_SETTINGS_FILE):
            data = json.loads(open(CLOCK_SETTINGS_FILE).read())
            if data.get("mode") in ("single", "quad"):
                result["mode"] = data["mode"]
            z = data.get("zones")
            if isinstance(z, list) and z:
                zones = [(str(e[0]), str(e[1])) for e in z[:4]
                         if isinstance(e, (list, tuple)) and len(e) >= 2]
                if zones:
                    result["zones"] = zones
    except Exception:
        pass  # any read/parse error → safe defaults
    _clock_settings_cache = result
    _clock_settings_cache_time = time.time()
    return result

def _best_clock_font(draw, text, max_w, sizes=(24, 22, 20, 18, 16, 14)):
    """Largest font from `sizes` whose rendered width fits within max_w."""
    for s in sizes:
        f = get_font(s)
        bb = draw.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
    return get_font(sizes[-1])

def _ring_progress(draw, x0, y0, x1, y1, frac, color):
    """Draw `frac` (0..1) of a rectangular border, clockwise from the top-left,
    using one line per edge (sub-pixel smooth, very cheap). Returns the tip
    (x, y) of the leading edge so the caller can add a glow head."""
    w, h = x1 - x0, y1 - y0
    edges = [((x0, y0), (x1, y0), w),   # top    →
             ((x1, y0), (x1, y1), h),   # right  ↓
             ((x1, y1), (x0, y1), w),   # bottom ←
             ((x0, y1), (x0, y0), h)]   # left   ↑
    total = 2 * (w + h)
    lit = frac * total
    tip = (x0, y0)
    for (ax, ay), (bx, by), length in edges:
        if lit <= 0 or length <= 0:
            continue
        seg = min(lit, length)
        t = seg / length
        ex = round(ax + (bx - ax) * t)
        ey = round(ay + (by - ay) * t)
        draw.line([(ax, ay), (ex, ey)], fill=color)
        tip = (ex, ey)
        lit -= seg
    return tip

def render_clock_single():
    """Big bold Eastern H:MM centred, with a thin border bar that sweeps
    smoothly around the frame once per minute (a glowing 'second hand')."""
    img = Image.new("RGB", (MATRIX_WIDTH, MATRIX_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        dt = datetime.now(ZoneInfo(SINGLE_CLOCK_TZ))
    except Exception:
        dt = datetime.now()
    h12  = dt.hour % 12 or 12
    tstr = str(h12) + ":" + ("%02d" % dt.minute)
    ampm = "AM" if dt.hour < 12 else "PM"
    frac = (dt.second + dt.microsecond / 1e6) / 60.0   # 0..1 around the minute

    W, H = MATRIX_WIDTH, MATRIX_HEIGHT
    track = (14, 22, 34)        # dim full-border "track"
    bar   = (0, 170, 255)       # swept portion (cyan)
    head  = (210, 245, 255)     # bright leading head

    # Dim 2px track around the whole frame.
    draw.rectangle([(0, 0), (W - 1, H - 1)], outline=track)
    draw.rectangle([(1, 1), (W - 2, H - 2)], outline=track)
    # Bright swept portion (two concentric rings = 2px thick) + glow head.
    tip = _ring_progress(draw, 0, 0, W - 1, H - 1, frac, bar)
    _ring_progress(draw, 1, 1, W - 2, H - 2, frac, bar)
    if frac > 0:
        draw.ellipse([(tip[0] - 1, tip[1] - 1), (tip[0] + 1, tip[1] + 1)], fill=head)

    # Big bold time, centred and nudged up to leave room for AM/PM.
    f = _best_clock_font(draw, tstr, W - 8)
    draw.text((W // 2, H // 2 - 4), tstr, font=f, fill=(255, 255, 255), anchor="mm")
    # AM/PM in the bar colour to tie the design together (subtle, below the time).
    draw.text((W // 2, H // 2 + 13), ampm, font=get_font(7), fill=(90, 150, 200), anchor="mm")
    return img

def render_clock_quad(zones):
    """The classic 2×2 grid showing up to four time zones."""
    img = Image.new("RGB", (MATRIX_WIDTH, MATRIX_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    lf = get_font(7); tf = get_font(9); sf = get_font(7)
    for i, (label, tz) in enumerate((zones or _DEFAULT_CLOCK_ZONES)[:4]):
        color = _CLOCK_SLOT_COLORS[i]
        col = i % 2; row = i // 2
        x = col*32 + 1; y = row*32
        try:
            dt = datetime.now(ZoneInfo(tz))
            h12 = dt.hour%12 or 12
            ts = str(h12)+":"+("%02d"%dt.minute)
            ss = ":"+("%02d"%dt.second)
        except Exception: ts = "?"; ss = ""
        bbox = draw.textbbox((0,0),label,font=lf); w = bbox[2]-bbox[0]
        draw.text((x+(32-w)//2, y+1), label, font=lf, fill=color)
        bbox = draw.textbbox((0,0),ts,font=tf); w = bbox[2]-bbox[0]
        draw.text((x+(32-w)//2, y+10), ts, font=tf, fill=(255,255,255))
        bbox = draw.textbbox((0,0),ss,font=sf); w = bbox[2]-bbox[0]
        draw.text((x+(32-w)//2, y+22), ss, font=sf, fill=(160,160,160))
    draw.line([(32,0),(32,63)], fill=(40,40,60))
    draw.line([(0,32),(63,32)], fill=(40,40,60))
    return img

def render_clock():
    """Dispatch to the single-zone (Eastern + seconds bar) or 4-zone grid based
    on the web-controlled clock setting."""
    s = _load_clock_settings()
    if s.get("mode") == "quad":
        return render_clock_quad(s.get("zones"))
    return render_clock_single()

def clock_is_single():
    """True when the clock slide is in single-Eastern (animated) mode."""
    return _load_clock_settings().get("mode") != "quad"

def animate_clock_frames(duration=0.6, fps=20):
    """Render the single clock smoothly for ~`duration` seconds, then yield back
    to the main loop so interrupts/slot timing are re-checked. Uses absolute-time
    scheduling: the frame send (_send_raw) blocks until the panel's next vsync, so
    stacking a fixed sleep on top produced an uneven cadence (visible stutter).
    Here each frame targets a fixed wall-clock slot and we only sleep for the time
    that's actually left, giving a steady, even sweep."""
    interval = 1.0 / fps
    end  = time.time() + duration
    nextf = time.time()
    while time.time() < end:
        display_pil_image(render_clock())
        nextf += interval
        rem = nextf - time.time()
        if rem > 0:
            time.sleep(rem)
        else:
            nextf = time.time()   # fell behind — resync rather than burst-catch-up

def render_weather(weather, aqi_data=None, frame=0):
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT),(0,0,0))
    draw = ImageDraw.Draw(img)
    if not weather or not weather.get("days"):
        draw.text((10,28),"loading...",font=get_font(8),fill=(150,150,150))
        return img
    tiny = get_font(7); small = get_font(7)
    cur = weather.get("current_temp")
    if cur is not None:
        ts = str(int(round(cur)))+chr(176)
        draw.text((1,0), ts, font=get_font(16), fill=(255,255,255))
    dim = (100,100,125)
    draw.text((34,1), "AQI", font=get_font(6), fill=dim)
    aqi = aqi_data.get("aqi") if aqi_data else None
    if aqi is not None:
        col,_ = aqi_color(aqi); aval = str(int(round(aqi)))
    else: col = (120,120,120); aval = "--"
    bbox = draw.textbbox((0,0),aval,font=get_font(9)); w = bbox[2]-bbox[0]
    draw.text((63-w, 0), aval, font=get_font(9), fill=col)
    draw.text((34,11), "UV", font=get_font(6), fill=dim)
    uv = weather.get("current_uv")
    if uv is not None:
        col,lbl = uv_color(uv); uval = str(int(round(uv)))+" "+lbl
    else: col = (120,120,120); uval = "--"
    bbox = draw.textbbox((0,0),uval,font=get_font(7)); w = bbox[2]-bbox[0]
    draw.text((63-w, 10), uval, font=get_font(7), fill=col)
    draw.line([(0,20),(63,20)], fill=(50,50,80))
    days = weather["days"][:3]
    cw = 21
    for i,day in enumerate(days):
        x0 = i*cw; cx = x0 + cw//2
        lbl = day.get("label","?")[:5]
        bbox = draw.textbbox((0,0),lbl,font=tiny); w = bbox[2]-bbox[0]
        c = (255,255,80) if i==0 else (160,195,255)
        draw.text((cx-w//2, 22), lbl, font=tiny, fill=c)
        draw_small_weather_icon(draw, cx, 39, day.get("code"), frame)
        hi = day.get("high")
        if hi is not None:
            ht = str(int(round(hi)))+chr(176)
            bbox = draw.textbbox((0,0),ht,font=tiny); w = bbox[2]-bbox[0]
            draw.text((cx-w//2,49), ht, font=tiny, fill=(255,170,60))
        lo = day.get("low")
        if lo is not None:
            lt = str(int(round(lo)))+chr(176)
            bbox = draw.textbbox((0,0),lt,font=tiny); w = bbox[2]-bbox[0]
            draw.text((cx-w//2,57), lt, font=tiny, fill=(100,170,255))
        if i < 2:
            draw.line([((i+1)*cw,21),((i+1)*cw,63)], fill=(35,35,60))
    return img

def render_sun(sun_data, weather=None):
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    hourly_uv = (weather or {}).get("hourly_uv", [])
    sr        = (sun_data or {}).get("sunrise")
    ss        = (sun_data or {}).get("sunset")
    now_dt    = datetime.now(ZoneInfo(LOCATION_TZ))
    now_hour  = now_dt.hour

    def _uv_col(uv):
        if uv < 3:  return (30, 160,  60)
        if uv < 6:  return (200, 190,   0)
        if uv < 8:  return (230, 110,   0)
        if uv < 11: return (210,  40,  20)
        return (160, 0, 200)

    def _fmt(dt):
        try: return str(dt.hour % 12 or 12) + ":" + ("%02d" % dt.minute)
        except Exception: return "--:--"

    # Compute peak UV
    peak_uv = None; peak_hour = 0
    if len(hourly_uv) >= 24:
        vals      = [float(v or 0) for v in hourly_uv[:24]]
        peak_uv   = max(vals)
        peak_hour = vals.index(peak_uv)

    # =========================================================
    # HEADER  y=0-16  (17px)
    # Left half: sunrise time  |  Right half: sunset time
    # =========================================================
    draw.line([(32, 0), (32, 20)], fill=(35, 35, 55))

    # Labels  y=0 (font 6)
    draw.text((2, 0), "SUNRISE", font=get_font(6), fill=(255, 200, 60))
    sb = draw.textbbox((0,0), "SUNSET", font=get_font(6))
    draw.text((63 - (sb[2]-sb[0]), 0), "SUNSET", font=get_font(6), fill=(255, 120, 40))

    # Large times  y=7 (font 11, ~13px tall -> fits y=7-20)
    if sr:
        ts = _fmt(sr)
        bb = draw.textbbox((0,0), ts, font=get_font(11)); tw = bb[2]-bb[0]
        draw.text((16 - tw//2, 7), ts, font=get_font(11), fill=(255, 222, 80))
    if ss:
        ts = _fmt(ss)
        bb = draw.textbbox((0,0), ts, font=get_font(11)); tw = bb[2]-bb[0]
        draw.text((48 - tw//2, 7), ts, font=get_font(11), fill=(255, 145, 55))

    draw.line([(0, 21), (63, 21)], fill=(40, 40, 62))

    # =========================================================
    # UV GRAPH  y=22-43  (22px tall)
    # =========================================================
    GX0, GX1, GY0, GY1 = 2, 62, 22, 43
    GW, GH = GX1 - GX0, GY1 - GY0
    MAX_UV = 11.0

    def _hour_x(h):
        return GX0 + int(h / 23.0 * GW)

    if len(hourly_uv) >= 24:
        pts = []
        for h in range(24):
            uv = max(0.0, float(hourly_uv[h] or 0))
            x  = _hour_x(h)
            y  = GY1 - int(min(uv, MAX_UV) / MAX_UV * GH)
            pts.append((x, max(GY0, min(GY1, y))))

        # Subtle horizontal grid at UV 3, 6, 9
        for mark in [3, 6, 9]:
            ym = GY1 - int(mark / MAX_UV * GH)
            draw.line([(GX0, ym), (GX1, ym)], fill=(28, 28, 46))

        # Filled area + coloured line
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]; x2, y2 = pts[i+1]
            uv  = float(hourly_uv[i] or 0)
            col = _uv_col(uv)
            dim = (col[0]//7, col[1]//7, col[2]//7)
            span = max(x2 - x1, 1)
            for x in range(x1, x2+1):
                t     = (x - x1) / span
                y_top = int(y1 + (y2 - y1) * t)
                y_top = max(GY0, min(GY1, y_top))
                if y_top < GY1:
                    draw.line([(x, y_top+1), (x, GY1)], fill=dim)
            draw.line([(x1,y1),(x2,y2)], fill=col, width=1)

        # Peak dot (colored) — 4×4 bounding box for a slightly chunky dot
        if peak_uv is not None:
            px, py = pts[peak_hour]
            draw.ellipse([(px-1,py-1),(px+2,py+2)], fill=_uv_col(peak_uv))

        # "Now" marker  — vertical dim line + white dot
        if 0 <= now_hour <= 23:
            nx, ny = pts[now_hour]
            draw.line([(nx, GY0), (nx, GY1)], fill=(48, 48, 68))
            draw.ellipse([(nx-1,ny-1),(nx+2,ny+2)], fill=(255,255,255))
    else:
        draw.text((32, 30), "loading...", font=get_font(6), fill=(55,55,75), anchor="mm")

    # =========================================================
    # X-AXIS  y=44-50
    # =========================================================
    for hr, lbl in [(6,"6a"),(12,"12p"),(18,"6p")]:
        tx = _hour_x(hr)
        draw.line([(tx, GY1), (tx, GY1+2)], fill=(55,55,80))
        lb = draw.textbbox((0,0), lbl, font=get_font(5)); lw = lb[2]-lb[0]
        draw.text((tx - lw//2, 45), lbl, font=get_font(5), fill=(105,105,155))

    draw.line([(0, 51), (63, 51)], fill=(38, 38, 58))

    # =========================================================
    # PEAK UV  y=52-63  (12px)
    # =========================================================
    # Label centred at y=52
    lb = draw.textbbox((0,0), "PEAK UV", font=get_font(5)); lw = lb[2]-lb[0]
    draw.text((32 - lw//2, 52), "PEAK UV", font=get_font(5), fill=(78,78,115))

    if peak_uv is not None:
        pcol = _uv_col(peak_uv)
        # Value  left-aligned, y=57
        pvs  = str(round(peak_uv, 1))
        bbox = draw.textbbox((0,0), pvs, font=get_font(7)); vw = bbox[2]-bbox[0]
        draw.text((3, 57), pvs, font=get_font(7), fill=pcol)
        # Time  right-aligned, y=57
        ph   = peak_hour % 12 or 12
        ampm = "am" if peak_hour < 12 else "pm"
        ts   = str(ph) + ":00" + ampm
        tb   = draw.textbbox((0,0), ts, font=get_font(7)); tw = tb[2]-tb[0]
        draw.text((63 - tw, 57), ts, font=get_font(7), fill=(178,178,218))

    return img


def render_aqi(aqi_data):
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT),(0,0,0))
    draw = ImageDraw.Draw(img)
    if not aqi_data:
        draw.text((10,28),"loading...",font=get_font(8),fill=(150,150,150))
        return img
    aqi = aqi_data.get("aqi")
    col, lbl = aqi_color(aqi)
    draw.text((2,0),"AIR QUALITY",font=get_font(7),fill=(180,200,255))
    draw.line([(0,9),(63,9)], fill=(40,40,60))
    if aqi is not None:
        big = get_font(22)
        ts = str(int(round(aqi)))
        bbox = draw.textbbox((0,0),ts,font=big); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, 11), ts, font=big, fill=col)
    bbox = draw.textbbox((0,0),lbl,font=get_font(9)); w = bbox[2]-bbox[0]
    draw.text(((64-w)//2, 36), lbl, font=get_font(9), fill=col)
    pm25 = aqi_data.get("pm25")
    if pm25 is not None:
        draw.text((2,48),"PM2.5",font=get_font(7),fill=(180,180,200))
        draw.text((28,48),str(round(pm25,1)),font=get_font(7),fill=(255,255,255))
    pm10 = aqi_data.get("pm10")
    if pm10 is not None:
        draw.text((2,56),"PM10",font=get_font(7),fill=(180,180,200))
        draw.text((28,56),str(round(pm10,1)),font=get_font(7),fill=(255,255,255))
    return img

def render_iss(iss):
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT),(0,0,5))
    draw = ImageDraw.Draw(img)

    # Flickering stars — seed changes every 0.4s so they twinkle
    tick = int(time.time() * 2.5)
    rg = random.Random(tick)
    star_positions = random.Random(99)  # fixed positions, only brightness flickers
    for _ in range(28):
        sx = star_positions.randint(0, 63)
        sy = star_positions.randint(0, 63)
        flicker_seed = (sx * 97 + sy * 31 + tick) % 7
        if flicker_seed < 5:
            sb = rg.randint(120, 255)
            draw.point((sx, sy), fill=(sb, sb, min(sb + 20, 255)))

    # ── Header (y=0–12) ──────────────────────────────────────────────────
    draw.text((32, 4),  "ISS",      font=get_font(8), fill=(100, 220, 255), anchor="mm")
    draw.text((32, 12), "OVERHEAD", font=get_font(6), fill=(255, 200, 80),  anchor="mm")

    # ── ISS silhouette — centered at (32, 24) ────────────────────────────
    cx, cy = 32, 24
    truss_col  = (170, 175, 180)
    panel_col  = (45, 85, 185)
    cell_col   = (22, 45, 105)
    module_col = (200, 205, 210)

    draw.line([(cx - 16, cy), (cx + 16, cy)], fill=truss_col, width=2)

    for side in (-1, 1):
        for offset in (5, 13):
            px = cx + side * offset
            x0, x1 = px - 2, px + 2
            y0, y1 = cy - 5, cy + 5
            draw.rectangle([(x0, y0), (x1, y1)], fill=panel_col)
            for gy in range(y0 + 3, y1, 3):
                draw.line([(x0, gy), (x1, gy)], fill=cell_col)

    draw.rectangle([(cx - 3, cy - 3), (cx + 3, cy + 3)], fill=module_col)
    draw.line([(cx - 3, cy), (cx + 3, cy)], fill=(150, 155, 160))

    draw.rectangle([(cx - 1, cy - 6), (cx + 1, cy - 4)], fill=(220, 225, 230))
    draw.rectangle([(cx - 1, cy + 4), (cx + 1, cy + 6)], fill=(220, 225, 230))

    # ── Divider ──────────────────────────────────────────────────────────
    draw.line([(6, 34), (57, 34)], fill=(35, 35, 60))

    # ── Info — three centred lines, all anchor="mm" ──────────────────────
    #   font7 ≈8px tall, font6 ≈6px tall. Lines at y=43/52/60:
    #   dist  43 → spans 39-47 | speed 52 → 49-55 | alt 60 → 57-63
    if iss and iss.distance is not None:
        dist_str = f"{int(iss.distance):,} mi away"
        draw.text((32, 43), dist_str, font=get_font(7), fill=(255, 255, 255), anchor="mm")
    draw.text((32, 52), "17,500 mph", font=get_font(6), fill=(120, 180, 255), anchor="mm")
    draw.text((32, 60), "~250 mi up", font=get_font(6), fill=(100, 140, 220), anchor="mm")
    return img

def render_flight_image(plane, route):
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT),(0,0,0))
    draw = ImageDraw.Draw(img)
    cs   = plane["callsign"]
    is_heli = _is_helicopter(plane)
    ai   = route.get("airline_icao")
    iata = route.get("airline_iata")
    # Only try airline logo for fixed-wing; helicopters rarely have IATA codes
    if not is_heli:
        if not ai and len(cs) >= 3:
            p3 = cs[:3].upper()
            if p3.isalpha(): ai = p3
        if ai and not iata:
            iata = ICAO_TO_IATA.get(ai)
    # ── Top-left 28×28 icon box ─────────────────────────────────────
    logo = get_airline_logo(ai, iata) if (ai and not is_heli) else None
    drawn = False
    if logo:
        try:
            logo = logo.convert("RGBA")
            logo.thumbnail((26,26), Image.Resampling.LANCZOS)
            bg = Image.new("RGB",(28,28),(255,255,255))
            bg.paste(logo, ((28-logo.width)//2, (28-logo.height)//2), logo)
            img.paste(bg, (0,0))
            drawn = True
        except Exception: pass  # bad logo image — fall through to text/silhouette fallback
    if not drawn:
        if not is_heli and ai and ai in AIRLINE_BRANDS:
            text,fg,bgc = AIRLINE_BRANDS[ai]
            draw.rectangle([(0,0),(27,27)], fill=bgc)
            bf = get_font(16) if len(text) <= 2 else get_font(12)
            bbox = draw.textbbox((0,0),text,font=bf)
            w = bbox[2]-bbox[0]; h = bbox[3]-bbox[1]
            draw.text(((28-w)//2-bbox[0],(28-h)//2-bbox[1]),text,font=bf,fill=fg)
        else:
            # Helicopter gets a teal background with 🚁; fixed-wing unknown gets dark with ✈
            bg_col = (0,30,30) if is_heli else (20,20,35)
            icon_char = "🚁" if is_heli else "✈"
            icon_color = (0,220,180) if is_heli else (190,215,255)
            draw.rectangle([(0,0),(27,27)], fill=bg_col)
            rendered = False
            for fp in [
                "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]:
                if os.path.exists(fp):
                    try:
                        ef = ImageFont.truetype(fp, 20)
                        bbox = draw.textbbox((0,0),icon_char,font=ef)
                        w = bbox[2]-bbox[0]; h = bbox[3]-bbox[1]
                        draw.text(((28-w)//2-bbox[0],(28-h)//2-bbox[1]),icon_char,font=ef,fill=icon_color)
                        rendered = True
                        break
                    except Exception: pass  # font doesn't support this glyph — try next
            if not rendered:
                if is_heli:
                    # Pixel-art helicopter silhouette
                    cx, cy = 14, 14; c = (0,220,180)
                    # rotor blade (horizontal bar)
                    draw.rectangle([(cx-10,cy-8),(cx+10,cy-7)], fill=c)
                    # body (oval)
                    draw.ellipse([(cx-6,cy-5),(cx+6,cy+4)], fill=c)
                    # tail boom
                    draw.rectangle([(cx+4,cy-2),(cx+10,cy-1)], fill=c)
                    # tail rotor
                    draw.rectangle([(cx+9,cy-5),(cx+10,cy+2)], fill=c)
                    # skids
                    draw.line([(cx-7,cy+6),(cx+5,cy+6)], fill=c)
                else:
                    cx, cy = 14, 14; c = (190,215,255)
                    draw.ellipse([(cx-2,cy-10),(cx+2,cy+10)], fill=c)
                    draw.polygon([(cx-11,cy+3),(cx+11,cy+3),(cx+2,cy-2),(cx-2,cy-2)], fill=c)
                    draw.polygon([(cx-5,cy+8),(cx+5,cy+8),(cx+2,cy+5),(cx-2,cy+5)], fill=c)
    sm = get_font(7)
    actype = plane.get("type","")
    type_label = actype[:6] if actype else ("HELI" if is_heli else "")
    type_color = (0,220,180) if is_heli else (180,220,255)
    # Top right: always show callsign, then type and speed below
    bbox = draw.textbbox((0,0),cs[:8],font=sm); w = bbox[2]-bbox[0]
    draw.text((30+(34-w)//2, 2), cs[:8], font=sm, fill=(255,220,100))
    if type_label:
        bbox = draw.textbbox((0,0),type_label,font=sm); w = bbox[2]-bbox[0]
        draw.text((30+(34-w)//2, 12), type_label, font=sm, fill=type_color)
    spd = str(plane["speed_mph"])+"mph"
    bbox = draw.textbbox((0,0),spd,font=sm); w = bbox[2]-bbox[0]
    draw.text((30+(34-w)//2, 21), spd, font=sm, fill=(255,255,255))
    # Divider
    draw.line([(0,30),(63,30)], fill=(40,40,60))
    med = get_font(8); sm2 = get_font(6)
    org = route.get("origin"); dst = route.get("dest")
    if org and dst:
        # Both ends known: origin (green) / arrow / dest (red)
        full = AIRPORT_NAMES.get(org, org)[:11]
        bbox = draw.textbbox((0,0),full,font=med); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, 32), full, font=med, fill=(0,255,100))
        draw.polygon([(28,42),(36,42),(32,46)], fill=(255,200,0))
        full = AIRPORT_NAMES.get(dst, dst)[:11]
        bbox = draw.textbbox((0,0),full,font=med); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, 47), full, font=med, fill=(255,100,100))
    elif org:
        # Only origin — show "FROM" label + airport centred in the space
        draw.text((32, 36), "FROM", font=sm2, fill=(90,90,110), anchor="mm")
        full = AIRPORT_NAMES.get(org, org)[:11]
        bbox = draw.textbbox((0,0),full,font=med); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, 44), full, font=med, fill=(0,235,90))
    elif dst:
        # Only destination — show "TO" label + airport centred in the space
        draw.text((32, 36), "TO", font=sm2, fill=(90,90,110), anchor="mm")
        full = AIRPORT_NAMES.get(dst, dst)[:11]
        bbox = draw.textbbox((0,0),full,font=med); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, 44), full, font=med, fill=(255,110,110))
    else:
        # No route data — show speed + heading as useful filler
        spd2 = str(plane["speed_mph"]) + " mph"
        hdg2 = "HDG " + str(int(plane.get("heading",0))) + "°"
        bbox = draw.textbbox((0,0),spd2,font=med); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, 36), spd2, font=med, fill=(200,200,200))
        bbox = draw.textbbox((0,0),hdg2,font=sm2); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, 47), hdg2, font=sm2, fill=(120,140,180))
    # Bottom row: heading arrow left, altitude text right
    draw_heading_arrow(draw, 6, 58, 5, plane["heading"])
    alt_str = str(plane["altitude_ft"])+"ft"
    bbox = draw.textbbox((0,0),alt_str,font=sm); w = bbox[2]-bbox[0]
    draw.text((63-w, 55), alt_str, font=sm, fill=(180,220,255))
    return img

def render_flight_detail(plane, route):
    """Detail card for manually tracked flights — city names, ETA, distance."""
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    tz = ZoneInfo(LOCATION_TZ)
    cs = plane["callsign"]

    # Header: callsign
    draw.text((32, 4), cs, font=get_font(9), fill=(255, 255, 255), anchor="mm")

    # Aircraft type + altitude
    parts = []
    if plane.get("type"): parts.append(plane["type"])
    if plane.get("altitude_ft"): parts.append(str(plane["altitude_ft"]) + "ft")
    if parts:
        draw.text((32, 13), "  ".join(parts), font=get_font(6), fill=(120, 160, 200), anchor="mm")

    draw.line([(2, 18), (61, 18)], fill=(40, 40, 60))

    org = route.get("origin"); dst = route.get("dest")
    if org and dst:
        draw.text((32, 25), org + " → " + dst, font=get_font(9), fill=(255, 200, 0), anchor="mm")
        org_city = (route.get("origin_city") or org)[:12]
        dst_city = (route.get("dest_city") or dst)[:12]
        draw.text(( 2, 34), org_city, font=get_font(7), fill=( 60, 200, 100), anchor="lm")
        draw.text((62, 43), dst_city, font=get_font(7), fill=(255, 100, 100), anchor="rm")
    elif org:
        draw.text((32, 23), "DEPARTING", font=get_font(6), fill=(90, 90, 110), anchor="mm")
        draw.text((32, 32), org, font=get_font(11), fill=(80, 255, 130), anchor="mm")
        city = (route.get("origin_city") or "")[:14]
        if city:
            draw.text((32, 43), city, font=get_font(7), fill=(60, 180, 90), anchor="mm")
    elif dst:
        draw.text((32, 23), "ARRIVING", font=get_font(6), fill=(90, 90, 110), anchor="mm")
        draw.text((32, 32), dst, font=get_font(11), fill=(255, 120, 100), anchor="mm")
        city = (route.get("dest_city") or "")[:14]
        if city:
            draw.text((32, 43), city, font=get_font(7), fill=(220, 90, 90), anchor="mm")
    else:
        draw.text((32, 25), "ENROUTE", font=get_font(9), fill=(100, 100, 120), anchor="mm")
        draw.text((32, 36), str(plane.get("speed_mph",0)) + " mph", font=get_font(7), fill=(160,160,180), anchor="mm")

    draw.line([(2, 49), (61, 49)], fill=(40, 40, 60))

    # ETA — calculate from plane position + speed + destination coords
    dest_lat = route.get("dest_lat")
    dest_lon = route.get("dest_lon")
    speed = plane.get("speed_mph", 0)
    if dest_lat and dest_lon and speed > 50:
        import datetime as _dt
        dist_rem = haversine_miles(plane["lat"], plane["lon"], float(dest_lat), float(dest_lon))
        hours_rem = dist_rem / speed
        eta_dt = datetime.now(tz) + _dt.timedelta(hours=hours_rem)
        eta_str = eta_dt.strftime("%-I:%M %p")
        dist_str = str(int(dist_rem)) + "mi"
        mins = int(hours_rem * 60)
        h2, m2 = divmod(mins, 60)
        time_str = (str(h2) + "h " if h2 else "") + str(m2) + "m"
        draw.text((32, 55), "ETA  " + eta_str, font=get_font(8), fill=(255, 255, 255), anchor="mm")
        draw.text((32, 62), dist_str + " · " + time_str, font=get_font(6), fill=(160, 160, 160), anchor="mm")
    else:
        draw.text((32, 55), str(speed) + " mph", font=get_font(8), fill=(255, 255, 255), anchor="mm")
        draw.text((32, 62), "hdg " + str(int(plane.get("heading", 0))) + "°", font=get_font(6), fill=(160, 160, 160), anchor="mm")

    return img

def render_now_playing(track, artist, source):
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT),(0,0,0))
    draw = ImageDraw.Draw(img)
    src_color = (30,215,96) if source == "spotify" else (255,255,255)
    src_label = "SPOTIFY" if source == "spotify" else "HOMEPOD"
    bbox = draw.textbbox((0,0),src_label,font=get_font(7)); w = bbox[2]-bbox[0]
    draw.text(((64-w)//2, 1), src_label, font=get_font(7), fill=src_color)
    draw.line([(0,11),(63,11)], fill=(40,40,60))
    tf = get_font(8)
    y = 15
    for chunk in [track[i:i+9] for i in range(0, min(len(track),18), 9)]:
        bbox = draw.textbbox((0,0),chunk,font=tf); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, y), chunk, font=tf, fill=(255,255,255))
        y += 10
    y += 2
    sf = get_font(7)
    for chunk in [artist[i:i+10] for i in range(0, min(len(artist),20), 10)]:
        bbox = draw.textbbox((0,0),chunk,font=sf); w = bbox[2]-bbox[0]
        draw.text(((64-w)//2, y), chunk, font=sf, fill=(180,180,200))
        y += 9
    # Animated equalizer bars — 5 bars, time-based heights
    bar_colors = [(0,200,80),(0,220,90),(30,235,100),(0,200,80),(0,180,70)]
    bar_w = 4; bar_gap = 3; total_w = 5*bar_w + 4*bar_gap
    bx = (64 - total_w) // 2
    by_base = 61
    tick = time.time()
    for i in range(5):
        freq = [1.3, 2.1, 1.7, 2.8, 1.1][i]
        phase = [0, 0.4, 0.8, 1.2, 1.6][i]
        h = int(4 + 5 * abs(math.sin(tick * freq + phase)))
        x0 = bx + i * (bar_w + bar_gap)
        draw.rectangle([(x0, by_base - h), (x0 + bar_w - 1, by_base)], fill=bar_colors[i])
    return img

def render_dual_music(sp_music, sp_art_path, hp_music, hp_art_path):
    """Split view when both Spotify and Apple Music are playing simultaneously."""
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    def _fill_32(art_path):
        if not art_path: return None
        try:
            art = Image.open(art_path).convert("RGB")
            w, h = art.size
            scale = max(32.0 / w, 32.0 / h)
            nw, nh = int(w * scale + 0.5), int(h * scale + 0.5)
            art = art.resize((nw, nh), Image.Resampling.LANCZOS)
            left = (nw - 32) // 2; top = (nh - 32) // 2
            return art.crop((left, top, left + 32, top + 32))
        except Exception: return None  # corrupt image file — show placeholder instead

    # Album arts
    sp_art = _fill_32(sp_art_path)
    if sp_art:
        img.paste(sp_art, (0, 0))
    else:
        draw.rectangle([(0, 0), (31, 31)], fill=(10, 35, 10))

    hp_art = _fill_32(hp_art_path)
    if hp_art:
        img.paste(hp_art, (32, 0))
    else:
        draw.rectangle([(32, 0), (63, 31)], fill=(25, 10, 10))

    # Dividers
    draw.line([(32, 0), (32, 63)], fill=(60, 60, 80))
    draw.line([(0, 32), (63, 32)], fill=(40, 40, 60))

    lf = get_font(6)

    # Left: "SPOTIFY" label
    bbox = draw.textbbox((0, 0), "SPOTIFY", font=lf); w = bbox[2] - bbox[0]
    draw.text((16 - w // 2, 45), "SPOTIFY", font=lf, fill=(30, 215, 96))

    # Right: "APPLE MUSIC" or "APPLE TV" label
    hp_source = (hp_music or {}).get("source", "homepod")
    line2 = "TV" if hp_source == "appletv" else "MUSIC"
    for txt, ypos, col in [("APPLE", 40, (200, 200, 210)), (line2, 50, (220, 220, 230))]:
        bbox = draw.textbbox((0, 0), txt, font=lf); w = bbox[2] - bbox[0]
        draw.text((48 - w // 2, ypos), txt, font=lf, fill=col)

    return img

# ========== PHOTOS ==========

def _load_photo_settings():
    try:
        with open(PHOTO_SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def zoom_to_fill_64(img, settings=None):
    if img.mode != "RGB": img = img.convert("RGB")
    s = settings or {}
    zoom   = float(s.get("zoom",   1.0))
    ox     = int(s.get("x",        0))
    oy     = int(s.get("y",        0))
    bright = float(s.get("brightness", 1.0))
    w, h   = img.size
    scale  = max(64.0 / w, 64.0 / h) * zoom
    nw, nh = int(w * scale + 0.5), int(h * scale + 0.5)
    img    = img.resize((max(nw,1), max(nh,1)), Image.Resampling.LANCZOS)
    left   = (nw - 64) // 2 - ox
    top    = (nh - 64) // 2 - oy
    left   = max(0, min(left, max(nw - 64, 0)))
    top    = max(0, min(top,  max(nh - 64, 0)))
    img    = img.crop((left, top, left + 64, top + 64))
    if img.size != (MATRIX_WIDTH,MATRIX_HEIGHT): img = img.resize((MATRIX_WIDTH,MATRIX_HEIGHT), Image.Resampling.LANCZOS)
    if bright != 1.0: img = ImageEnhance.Brightness(img).enhance(bright)
    return img

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

def get_random_photo():
    if not PHOTOS_DIR.exists(): return None
    candidates = [p for p in PHOTOS_DIR.iterdir()
                  if p.suffix.lower() in PHOTO_EXTENSIONS]
    if not candidates: return None
    return str(random.choice(candidates))

def pick_photo(forced=None):
    if forced:
        p = PHOTOS_DIR / os.path.basename(forced)
        if p.exists(): return str(p)
    return get_random_photo()

def count_photos():
    if not PHOTOS_DIR.exists(): return 0
    return sum(1 for p in PHOTOS_DIR.iterdir()
               if p.suffix.lower() in PHOTO_EXTENSIONS)

# ========== DISPLAY ==========
_display_proc = None
_last_raw_img = None
_display_lock = threading.Lock()  # Prevents concurrent writes to display (avoids flicker)

def get_display_proc():
    global _display_proc
    if _display_proc is None or _display_proc.poll() is not None:
        _display_proc = subprocess.Popen([DISPLAY_BIN], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _display_proc

def _send_raw(img):
    """Send image to display driver via PPM pipe. Thread-safe with lock."""
    with _display_lock:  # Prevent concurrent writes that cause flicker
        buf = BytesIO(); img.save(buf,"PPM")
        try:
            proc = get_display_proc()
            proc.stdin.write(buf.getvalue()); proc.stdin.flush()
        except Exception as e:
            print("Display pipe error: "+str(e))

def display_pil_image(img, photo=False):
    global _last_raw_img
    if img.mode != "RGB": img = img.convert("RGB")
    if img.size != (MATRIX_WIDTH,MATRIX_HEIGHT):
        canvas = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT),(0,0,0))
        img.thumbnail((MATRIX_WIDTH,MATRIX_HEIGHT), Image.Resampling.LANCZOS)
        canvas.paste(img, ((MATRIX_WIDTH-img.width)//2, (MATRIX_HEIGHT-img.height)//2))
        img = canvas
    # get_brightness() returns a float in [0.0, 1.0].  At low brightness (≤ 0.5,
    # i.e. the 35% or lower schedule segments) very-dark pixels look muddy rather
    # than black on the LED panel, so we crush them to true black here.
    # Bug-fix: the original threshold was 50, which is always true for any valid
    # brightness (max 1.0 ≤ 50), meaning crushing was applied at ALL brightness
    # levels including 100%.  Correct threshold is 0.5 (= 50%).
    if not photo and get_brightness() <= 0.5:
        img = img.point(lambda p: p if p > 22 else 0)
    _last_raw_img = img.copy()
    _send_raw(apply_dimming(img))

def do_transition(next_img=None):
    """Smooth fade: current slide → next slide (or black if next_img is None).
    16 frames at 30ms = ~0.5s total. Uses smoothstep for a gentle ease in/out."""
    global _last_raw_img
    if _last_raw_img is None: return
    b    = get_brightness()
    src  = _last_raw_img.copy()
    dst  = next_img if next_img is not None else Image.new("RGB", (MATRIX_WIDTH, MATRIX_HEIGHT), (0, 0, 0))
    if dst.size != src.size: dst = dst.resize(src.size)
    for i in range(1, 17):
        t   = i / 16.0
        t   = t * t * (3.0 - 2.0 * t)   # smoothstep
        img = Image.blend(src, dst, t)
        _send_raw(ImageEnhance.Brightness(img).enhance(b))
        time.sleep(0.030)

def do_globe_intro(reveal_img, flights_tracker=None, city_name=None):
    """Startup animation — real satellite texture, seamless Google-Earth-style zoom.

    Phases:
      1. Real satellite texture projected onto a spinning globe (one full
         rotation, ease-out, landing with the configured location facing viewer)
      2. Smooth zoom into the location — world texture crops tighter each frame
      3. Satellite tile of the exact area (fetched from ESRI World Imagery CDN,
         cached after first download) with slow green strobe on the pinpoint

    Projection used for the spin:
      Orthographic — viewed straight down the Z axis.
      For each screen pixel (px, py) inside the globe circle:
        u = (px - gcx) / r          # x normalised to [-1, 1]
        v = (gcy - py) / r          # y normalised (up = positive)
        z = sqrt(1 - u² - v²)       # depth (front hemisphere only)
      After rotating the globe by angle θ around the Y axis:
        X = u·cos θ + z·sin θ
        lon = atan2(X, z_rot)        → sample equirectangular texture
        lat = arcsin(v)
      Uses numpy for vectorised per-pixel calculation (~64×64 pixels,
      very fast on Pi 4).

    Fallback: if texture download fails and no cache exists, the function
    silently skips the animation (no crash, no hang).
    """
    import numpy as np

    brt = get_brightness()
    W, H   = MATRIX_WIDTH, MATRIX_HEIGHT
    cx, cy = W // 2, H // 2
    R      = 28           # globe radius in pixels

    LAT = math.radians(CHATTANOOGA_LAT)
    LON = math.radians(CHATTANOOGA_LON)

    # ── Coordinate grids — computed once, reused every frame ─────────────
    px_arr = np.arange(W, dtype=np.float32)
    py_arr = np.arange(H, dtype=np.float32)
    PX, PY = np.meshgrid(px_arr, py_arr)   # both shape (H, W)

    # ── Load / download Earth satellite texture ───────────────────────────
    TEXTURE_CACHE = CACHE_DIR / "earth_texture.jpg"
    TEXTURE_URLS  = [
        # NASA Blue Marble (2048×1024, equirectangular, public domain)
        "https://eoimages.gsfc.nasa.gov/images/imagerecords/57000/57752/land_shallow_topo_2048.jpg",
        # Wikimedia fallback (same projection, reliable CDN)
        "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cd/Land_ocean_ice_cloud_hires.jpg/1024px-Land_ocean_ice_cloud_hires.jpg",
    ]

    texture = None
    if TEXTURE_CACHE.exists():
        try:
            texture = Image.open(TEXTURE_CACHE).convert("RGB")
        except Exception:
            pass

    if texture is None:
        print("[Globe] Downloading Earth texture (first run, cached afterwards)…")
        for url in TEXTURE_URLS:
            try:
                resp = _session.get(url, timeout=15)
                if resp.status_code == 200:
                    raw = Image.open(BytesIO(resp.content)).convert("RGB")
                    # Resize to 540×270 — enough detail for 64px globe, fast to process
                    raw = raw.resize((540, 270), Image.LANCZOS)
                    raw.save(TEXTURE_CACHE, "JPEG", quality=92)
                    texture = raw
                    print("[Globe] Texture downloaded and cached.")
                    break
            except Exception as e:
                print(f"[Globe] Texture source failed: {e}")

    if texture is None:
        print("[Globe] No texture available — skipping intro animation.")
        return

    TW, TH   = texture.size                        # 540 × 270
    tex_arr  = np.array(texture, dtype=np.uint8)   # shape (TH, TW, 3)

    # ── Satellite close-up tile (ESRI World Imagery, cached) ─────────────
    # We fetch one 256×256 tile at zoom=10 centred on the configured location.
    # The tile URL scheme is standard XYZ/TMS.
    TILE_CACHE = CACHE_DIR / "location_tile.jpg"

    def _deg2tile(lat_deg, lon_deg, zoom):
        """Convert geographic coordinates to XYZ tile indices."""
        n   = 2 ** zoom
        x   = int((lon_deg + 180.0) / 360.0 * n)
        lat_r = math.radians(lat_deg)
        y   = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
        return x, y

    location_tile = None
    if TILE_CACHE.exists():
        try:
            location_tile = Image.open(TILE_CACHE).convert("RGB")
        except Exception:
            pass

    if location_tile is None:
        try:
            zoom    = 10
            tx, ty  = _deg2tile(CHATTANOOGA_LAT, CHATTANOOGA_LON, zoom)
            tile_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{ty}/{tx}"
            resp = _session.get(tile_url, timeout=8)
            if resp.status_code == 200:
                location_tile = Image.open(BytesIO(resp.content)).convert("RGB")
                location_tile.save(TILE_CACHE, "JPEG", quality=92)
                print("[Globe] Location satellite tile cached.")
        except Exception as e:
            print(f"[Globe] Location tile fetch failed: {e}")

    # ── Star field — fixed random positions so they don't flicker ────────
    # Generate once, reuse every frame. Each star is (x, y, brightness).
    _rng   = random.Random(42)   # fixed seed = same stars every boot
    _stars = [
        (_rng.randint(0, W-1), _rng.randint(0, H-1),
         _rng.randint(60, 200))
        for _ in range(60)
    ]

    # ── Core rendering function (numpy orthographic projection) ───────────
    def _render_textured_globe(rot_angle, gcx_f=float(cx), gcy_f=float(cy), radius=float(R)):
        """
        Project the Earth texture onto the globe at the given rotation angle.
        Each screen pixel inside the circle is back-projected to a lat/lon,
        then sampled from the equirectangular satellite texture.
        Pixels outside the circle stay black (space).
        """
        r = max(1.0, radius)

        # Normalised globe coordinates for every screen pixel
        U = (PX - gcx_f) / r   # shape (H, W)
        V = (gcy_f - PY) / r

        dist2 = U * U + V * V
        mask  = dist2 <= 1.0   # True for pixels inside the globe

        # Depth on the front hemisphere (Z > 0 = facing viewer)
        Z = np.where(mask, np.sqrt(np.maximum(0.0, 1.0 - dist2)), 0.0)

        # Rotate around Y axis by rot_angle
        cos_a = math.cos(rot_angle)
        sin_a = math.sin(rot_angle)
        X_rot =  U * cos_a + Z * sin_a
        Z_rot = -U * sin_a + Z * cos_a

        # Geographic coordinates (radians)
        lat_map = np.arcsin(np.clip(V, -1.0, 1.0))
        lon_map = np.arctan2(X_rot, Z_rot)   # range (-π, π)

        # Map to equirectangular texture pixel indices
        tex_x = np.mod(
            ((lon_map / (2.0 * math.pi) + 0.5) * TW).astype(np.int32), TW
        )
        tex_y = np.clip(
            ((0.5 - lat_map / math.pi) * TH).astype(np.int32), 0, TH - 1
        )

        # Limb darkening: dim pixels near the edge for a 3-D look
        # Z ranges 0 (limb) → 1 (centre); apply gentle power curve
        limb = np.where(mask, 0.35 + 0.65 * Z, 0.0)

        result = np.zeros((H, W, 3), dtype=np.float32)
        idx_y  = tex_y[mask]
        idx_x  = tex_x[mask]
        result[mask] = tex_arr[idx_y, idx_x].astype(np.float32)

        # Apply limb darkening per-channel
        result[:, :, 0] *= limb
        result[:, :, 1] *= limb
        result[:, :, 2] *= limb

        # Paint stars in the black space around the globe
        for sx, sy, sv in _stars:
            if not mask[sy, sx]:   # only draw outside the globe
                result[sy, sx] = [sv, sv, sv]

        return Image.fromarray(result.clip(0, 255).astype(np.uint8))



    # ── Tile utilities ────────────────────────────────────────────────────
    def _latlon_to_tile(lat, lon, z):
        """Convert lat/lon to XYZ tile coordinates at the given zoom level."""
        n     = 2 ** z
        tx    = int((lon + 180.0) / 360.0 * n)
        lat_r = math.radians(lat)
        ty    = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
        return tx, ty

    def _tile_to_latlon(tx, ty, z):
        """Top-left corner (lat, lon) of a tile."""
        n   = 2 ** z
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * ty / n))))
        return lat, lon

    def _latlon_to_pixel_in_mosaic(lat, lon, lat_top, lon_left, lat_bot, lon_right, mw, mh):
        """Map a lat/lon to pixel coordinates inside a tile mosaic using Mercator."""
        def merc(l): return math.log(math.tan(math.pi / 4 + math.radians(l) / 2))
        px = int((lon - lon_left)  / (lon_right - lon_left)  * mw)
        py = int((merc(lat_top) - merc(lat)) / (merc(lat_top) - merc(lat_bot)) * mh)
        return px, py

    def _fetch_mosaic(center_lat, center_lon, zoom, grid=3, cache_path=None, timeout=10):
        """Download a grid×grid tile mosaic centred on the location; cache to disk."""
        if cache_path and Path(cache_path).exists():
            try:
                return Image.open(cache_path).convert("RGB")
            except Exception:
                pass
        tx_c, ty_c = _latlon_to_tile(center_lat, center_lon, zoom)
        half   = grid // 2
        mosaic = Image.new("RGB", (grid * 256, grid * 256), (8, 8, 8))
        for row in range(grid):
            for col in range(grid):
                tx = tx_c - half + col
                ty = ty_c - half + row
                url = (f"https://server.arcgisonline.com/ArcGIS/rest/services/"
                       f"World_Imagery/MapServer/tile/{zoom}/{ty}/{tx}")
                try:
                    r = _session.get(url, timeout=timeout)
                    if r.status_code == 200:
                        mosaic.paste(Image.open(BytesIO(r.content)).convert("RGB"),
                                     (col * 256, row * 256))
                except Exception:
                    pass
        if cache_path:
            try: mosaic.save(cache_path, "JPEG", quality=92)
            except Exception: pass
        return mosaic

    def _mosaic_bounds(center_lat, center_lon, zoom, grid=3):
        """Return (lat_top, lon_left, lat_bot, lon_right) for a tile mosaic."""
        tx_c, ty_c = _latlon_to_tile(center_lat, center_lon, zoom)
        half = grid // 2
        lat_top, lon_left = _tile_to_latlon(tx_c - half,      ty_c - half,      zoom)
        lat_bot, lon_right = _tile_to_latlon(tx_c + half + 1, ty_c + half + 1,  zoom)
        return lat_top, lon_left, lat_bot, lon_right

    # ── Prefetch tiles in background during the spin ──────────────────────
    # Zoom-12: ~3km per tile, 3×3 = ~9km — good city-level view.
    # Zoom-10: ~20km per tile, 3×3 = ~60km — shows 25-mile plane radius.
    CITY_ZOOM  = 12
    PLANE_ZOOM = 10
    CITY_CACHE  = str(CACHE_DIR / "location_city_z12.jpg")
    PLANE_CACHE = str(CACHE_DIR / "location_planeview_z10.jpg")

    _city_box  = [None]
    _plane_box = [None]

    def _fetch_city():
        try:
            _city_box[0] = _fetch_mosaic(CHATTANOOGA_LAT, CHATTANOOGA_LON,
                                          CITY_ZOOM, grid=3, cache_path=CITY_CACHE)
            print("[Globe] City tiles ready.")
        except Exception as e:
            print(f"[Globe] City tiles failed: {e}")

    def _fetch_plane_view():
        try:
            _plane_box[0] = _fetch_mosaic(CHATTANOOGA_LAT, CHATTANOOGA_LON,
                                           PLANE_ZOOM, grid=3, cache_path=PLANE_CACHE)
            print("[Globe] Plane-view tiles ready.")
        except Exception as e:
            print(f"[Globe] Plane-view tiles failed: {e}")

    import threading as _thr
    _thr.Thread(target=_fetch_city,       daemon=True).start()
    _thr.Thread(target=_fetch_plane_view, daemon=True).start()

    # ── Phase 1: Spin one full rotation landing at the location ───────────
    SPIN_FRAMES = 80
    for i in range(SPIN_FRAMES):
        t     = i / (SPIN_FRAMES - 1)
        ease  = 1.0 - (1.0 - t) ** 2
        angle = LON + 2.0 * math.pi * (1.0 - ease)
        img   = _render_textured_globe(angle)
        _send_raw(ImageEnhance.Brightness(img).enhance(brt))
        time.sleep(0.033)

    # ── Phase 2: Seamless zoom to city level ──────────────────────────────
    # Scale goes from 1 → 14 (globe radius 28 → 392px on 64px screen).
    # At scale=14 only a tiny flat patch is visible — seamless like Google Earth.
    # Crossfades into the zoom-12 city satellite mosaic in the final third.
    MAX_SCALE   = 14.0
    ZOOM_FRAMES = 90
    BLEND_START = 62

    # Prepare city background: centre crop of 3×3 mosaic → 64×64
    city_bg = None
    if _city_box[0] is not None:
        cm  = _city_box[0]
        cw, ch = cm.size          # 768 × 768
        margin = cw // 4          # crop inner half — tighter city view
        city_bg = cm.crop((margin, margin, cw - margin, ch - margin)
                          ).resize((W, H), Image.LANCZOS)

    last_frame = None
    for i in range(ZOOM_FRAMES):
        t     = i / (ZOOM_FRAMES - 1)
        ease  = t * t * (3.0 - 2.0 * t)
        scale = 1.0 + (MAX_SCALE - 1.0) * ease

        gcy_z       = cy + R * scale * math.sin(LAT)
        globe_frame = _render_textured_globe(LON, gcx_f=float(cx),
                                             gcy_f=gcy_z, radius=R * scale)

        if city_bg is not None and i >= BLEND_START:
            bt    = (i - BLEND_START) / (ZOOM_FRAMES - BLEND_START)
            bt    = bt * bt * (3.0 - 2.0 * bt)
            frame = Image.blend(globe_frame, city_bg, bt)
        else:
            frame = globe_frame

        if i >= BLEND_START // 2:
            d  = ImageDraw.Draw(frame)
            gv = int(200 * ease)
            d.ellipse([(cx-1, cy-1), (cx+1, cy+1)], fill=(0, gv, gv//2))

        last_frame = frame
        _send_raw(ImageEnhance.Brightness(frame).enhance(brt))
        time.sleep(0.033)

    # ── Phase 3: City view — city name + green strobe ─────────────────────
    bg = city_bg if city_bg is not None else last_frame

    def _city_frame(pulse):
        f  = bg.copy()
        d  = ImageDraw.Draw(f)
        # Green strobe dot + glow ring
        glow_r = 2 + int(4 * pulse)
        glow_v = int(255 * pulse)
        d.ellipse([(cx-glow_r, cy-glow_r), (cx+glow_r, cy+glow_r)],
                  outline=(0, glow_v, glow_v // 3))
        d.ellipse([(cx-1, cy-1), (cx+1, cy+1)], fill=(0, 255, 90))
        return f

    STROBE_FRAMES = 55
    for i in range(STROBE_FRAMES):
        t     = i / STROBE_FRAMES
        pulse = 0.5 + 0.5 * math.sin(t * 2.0 * math.pi * 2.0)
        _send_raw(ImageEnhance.Brightness(_city_frame(pulse)).enhance(brt))
        time.sleep(0.040)

    # ── Slow fade to the first slideshow frame (the clock) ───────────────
    final_map = _city_frame(0.0)   # clean city frame with no glow
    FADE_FRAMES = 40
    for i in range(FADE_FRAMES):
        t     = i / (FADE_FRAMES - 1)
        ease  = t * t * (3.0 - 2.0 * t)   # smoothstep — gentle start and end
        frame = Image.blend(final_map, reveal_img, ease)
        _send_raw(ImageEnhance.Brightness(frame).enhance(brt))
        time.sleep(0.040)



def do_plane_transition(flight_img):
    """Animated intro for the flight tracker: a top-down airliner silhouette
    sweeps left→right across the matrix, wiping the previous slide away and
    revealing `flight_img` (the flight card) behind its wings.

    ┌──────────────────────────────────────────────────────────────────────┐
    │ WHY THIS RUNS SYNCHRONOUSLY (blocking ~3.2s) — DO NOT THREAD IT.       │
    │                                                                        │
    │ The LED matrix has exactly ONE writer pipe (_send_raw). If this        │
    │ animation runs in a background thread while the main loop also calls    │
    │ display_pil_image(), the two alternate frames on the panel and you get  │
    │ the flicker we chased for hours. Running it inline in the main loop     │
    │ guarantees a single, ordered stream of frames → perfectly smooth.      │
    │                                                                        │
    │ The startup intro (see main()) already calls this synchronously and    │
    │ looks great; the in-loop plane intro now uses the same path.           │
    │                                                                        │
    │ The animation's FINAL frame is exactly `flight_img` (plane has flown    │
    │ off the right edge, mask fully reveals the card), so the hand-off to    │
    │ the static flight card afterwards is seamless — no flash, no gap.       │
    └──────────────────────────────────────────────────────────────────────┘
    """
    b  = get_brightness()
    W, H = MATRIX_WIDTH, MATRIX_HEIGHT
    mid = H // 2   # 32
    col = (230, 235, 245)

    plane_len = 46   # nose to tail in pixels

    # Capture whatever is currently on screen as the outgoing slide
    from_img = _last_raw_img.copy() if _last_raw_img else Image.new("RGB", (W, H), (0, 0, 0))

    def pt(nose_x, dx, dy):
        """Screen coordinate relative to nose position."""
        return (nose_x + dx, mid + dy)

    frames = 55
    for frame in range(frames):
        t = frame / (frames - 1)
        # Nose to tail = 46px. For tail to clear screen (>= 64), nose must reach >= 110
        nose_x = int(t * (W + plane_len + 46)) - plane_len

        # Wing leading edge positions:
        #   wl_tip  = x where the wing tip (top/bottom edge) crosses the screen
        #   wl_root = x where the wing root meets the fuselage at mid
        wl_root = nose_x - 10
        wl_tip  = nose_x - 26

        tip_x  = max(0, min(wl_tip,  W))
        root_x = max(0, min(wl_root, W))

        # IMPORTANT: when tip_x == 0 the chevron polygon degenerates to a
        # vertical line at x=0 and PIL still fills a 1-pixel column — that's
        # the "nose dot" that appeared before the animation started.
        # Guard: if the chevron hasn't reached the screen yet, just show the
        # outgoing slide unchanged (no composite, no plane drawing).
        if tip_x == 0 and root_x == 0:
            _send_raw(ImageEnhance.Brightness(from_img).enhance(b))
            time.sleep(0.040)
            continue

        mask = Image.new("L", (W, H), 0)
        md   = ImageDraw.Draw(mask)
        md.polygon([
            (0,      0),
            (tip_x,  0),
            (root_x, mid),
            (tip_x,  H - 1),
            (0,      H - 1),
        ], fill=255)
        img = Image.composite(flight_img, from_img, mask)

        d  = ImageDraw.Draw(img)
        nx = nose_x

        # Only draw plane if any part is on screen
        if nx + 10 >= 0:  # tail is at nx-44, but nose area starts at nx
            # ── Fuselage ─────────────────────────────────────────────────────
            d.polygon([
                pt(nx,  0,  0),
                pt(nx, -3, -3),
                pt(nx,-41, -3),
                pt(nx,-44,  0),
                pt(nx,-41,  3),
                pt(nx, -3,  3),
            ], fill=col)

            # ── Main wings — leading edge IS the wipe boundary ───────────────
            d.polygon([             # upper wing
                pt(nx, -10, -3),
                pt(nx, -26, -mid),
                pt(nx, -30, -mid),
                pt(nx, -20, -3),
            ], fill=col)
            d.polygon([             # lower wing
                pt(nx, -10,  3),
                pt(nx, -26,  mid - 1),
                pt(nx, -30,  mid - 1),
                pt(nx, -20,  3),
            ], fill=col)

            # ── Engine nacelles ───────────────────────────────────────────────
            for ey, ew in [(-10, -7), (-21, -18)]:
                d.rectangle([pt(nx, -13, ey), pt(nx, -10, ew)], fill=col)
            for ey, ew in [(7, 10), (18, 21)]:
                d.rectangle([pt(nx, -13, ey), pt(nx, -10, ew)], fill=col)

            # ── Horizontal tail stabilizers ───────────────────────────────────
            d.polygon([pt(nx,-35,-3), pt(nx,-38,-3), pt(nx,-42,-12), pt(nx,-39,-12)], fill=col)
            d.polygon([pt(nx,-35, 3), pt(nx,-38, 3), pt(nx,-42, 12), pt(nx,-39, 12)], fill=col)

        _send_raw(ImageEnhance.Brightness(img).enhance(b))
        time.sleep(0.040)


_file_cache_path = None
_file_cache_img = None
def display_image_file(path):
    global _file_cache_path, _file_cache_img
    try:
        if path != _file_cache_path:
            _file_cache_img = None
            with Image.open(path) as _raw:
                _file_cache_img = _raw.convert("RGB").copy()
            _file_cache_path = path
        display_pil_image(_file_cache_img, photo=True)
    except Exception as e:
        print("Display error ("+str(path)+"): "+str(e))
        _file_cache_path = None

def download_art(image_url, prefix="art"):
    if not image_url: return None
    ck = hashlib.md5(image_url.encode()).hexdigest()
    cf = CACHE_DIR / (prefix+"_"+ck+".jpg")
    if cf.exists(): return str(cf)
    try:
        r = _session.get(image_url, timeout=3); r.raise_for_status()
        Image.open(BytesIO(r.content)).save(cf,"JPEG")
        return str(cf)
    except Exception as e:
        print("Art err: "+str(e)); return None

# ========== STARTUP GEOLOCATION ==========

def _detect_location():
    """One-time IP geolocation on startup.
    Returns (lat, lon, city_string) if successful, None on failure.
    Uses ip-api.com (free, no key required).
    """
    try:
        r = _session.get(
            "http://ip-api.com/json?fields=status,city,regionName,country,lat,lon",
            timeout=6)
        d = r.json()
        if d.get("status") == "success" and d.get("lat") and d.get("lon"):
            parts = [d.get("city",""), d.get("regionName",""), d.get("country","")]
            desc  = ", ".join(p for p in parts if p)
            return float(d["lat"]), float(d["lon"]), desc
    except Exception as e:
        print(f"Geolocation lookup failed: {e}")
    return None

# ========== FIRST-RUN / SETUP HELPERS ==========

def _get_local_ip():
    """Return the Pi's LAN IP (used to build the Spotify setup URL for the QR code)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"

def draw_qr_screen(url, top_label="SPOTIFY", bot_label="SCAN TO AUTH"):
    """Render a QR code + short labels on the LED matrix.
    The QR code encodes *url* (e.g. the Spotify setup page on the local network).
    """
    try:
        if not HAS_QR:
            print(f"QR: qrcode library not available. Visit: {url}")
            return
        img = Image.new("RGB", (MATRIX_WIDTH, MATRIX_HEIGHT), (0, 0, 10))
        draw = ImageDraw.Draw(img)

        # ── Generate QR image ──
        qr = _qrcode.QRCode(
            version=None,
            error_correction=_qrcode.constants.ERROR_CORRECT_L,
            box_size=2,
            border=0,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr_pil = qr.make_image(fill_color=(255, 255, 255),
                                back_color=(0, 0, 0)).convert("RGB")

        # ── Scale to fill as much of the matrix as possible, leaving room for labels ──
        label_h = 9          # px reserved top and bottom
        pad = 2              # px gap between label and QR
        avail_h = MATRIX_HEIGHT - 2 * (label_h + pad)
        avail_w = MATRIX_WIDTH - 4
        qw, qh = qr_pil.size
        scale = min(avail_w / qw, avail_h / qh)
        new_w = max(1, int(qw * scale))
        new_h = max(1, int(qh * scale))
        qr_pil = qr_pil.resize((new_w, new_h), Image.NEAREST)

        # ── Paste QR centred vertically in the available band ──
        x = (MATRIX_WIDTH - new_w) // 2
        y = label_h + pad + (avail_h - new_h) // 2
        img.paste(qr_pil, (x, y))

        # ── Labels ──
        font = get_font(7)
        for text, color, anchor_y in [
            (top_label, (30, 215, 96), 1),
            (bot_label, (30, 215, 96), MATRIX_HEIGHT - label_h),
        ]:
            bb = draw.textbbox((0, 0), text, font=font)
            tw = bb[2] - bb[0]
            draw.text(((MATRIX_WIDTH - tw) // 2, anchor_y), text, font=font, fill=color)

        display_pil_image(img)
    except Exception as e:
        print(f"draw_qr_screen error: {e}")

# ========== MAIN ==========
def main():
    """
    ═══════════════════════════════════════════════════════════════════════════════
                            MAIN DASHBOARD LOOP
    ═══════════════════════════════════════════════════════════════════════════════

    PERFORMANCE NOTES:
    ──────────────────
    • Main loop runs ~2 frames/sec (0.5s sleep), never blocks on network
    • All network calls (API polls, image downloads) run in background threads
    • Uses connection pooling to reuse TCP connections (huge performance win)
    • Caches: images, weather, flight routes, ISS position to minimize disk I/O
    • Thread-safe: uses locks for shared data (positions, cache, etc)

    FLOW:
    ─────
    1. Load Spotify config, initialize HomePod discovery, create API clients
    2. Main loop runs continuously:
       a) Poll background threads for data (non-blocking)
       b) Check for user overrides from web dashboard
       c) High-priority interrupts: flights, ISS, music (show immediately)
       d) Default: cycle through slides (clock → weather → sun → photos)
       e) Display image, sleep 0.5s, repeat
    3. All background threads (weather, flights, Spotify, HomePod) run async

    THREAD SAFETY:
    ──────────────
    • Locks used for: flight positions, route cache, display state
    • Threads are daemon so they exit when main process exits
    • No resource leaks: all connections reused via session pool
    """
    # ── First-run setup gate ──
    # On a brand-new install (no completed setup) show ONE QR pointing at the web
    # setup wizard and block here until the user finishes location / password /
    # Spotify. Once "setup_complete" is cached in ~/.dashboard_setup.json this
    # never runs again — the dashboard boots straight through on every reboot.
    if not _cfg.setup_is_complete():
        ip = _get_local_ip()
        setup_url = f"http://{ip}:{WEB_PORT}/setup"
        print(f"First-time setup required — scan the QR or visit {setup_url}")
        draw_qr_screen(setup_url, top_label="SETUP", bot_label="SCAN TO START")
        while not _cfg.setup_is_complete():
            time.sleep(3)
        print("Setup complete — starting dashboard")

    cf = Path(os.path.expanduser("~/.spotify_display.conf"))

    # ── Load config (may not exist on a brand-new install) ──
    config = {}
    if cf.exists():
        try:
            with open(cf) as f:
                config = json.load(f)
        except Exception as e:
            print(f"Config read error: {e}")

    # ── Spotify first-run OAuth flow ──
    # If we have credentials but no refresh_token, show a QR on the matrix so
    # the user can authorise from their phone without touching a console.
    if config.get("client_id") and config.get("client_secret") and not config.get("refresh_token"):
        ip = _get_local_ip()
        setup_url = f"http://{ip}:{WEB_PORT}/spotify/setup"
        print(f"Spotify not authorised — visit {setup_url} to connect")
        draw_qr_screen(setup_url, top_label="SPOTIFY", bot_label="SCAN TO AUTH")
        # Block here until the web callback saves the refresh_token
        while True:
            time.sleep(3)
            try:
                with open(cf) as f:
                    config = json.load(f)
                if config.get("refresh_token"):
                    print("Spotify authorised — starting dashboard")
                    break
            except Exception:
                pass
    elif not config.get("client_id"):
        ip = _get_local_ip()
        print(f"No Spotify config found. Visit http://{ip}:{WEB_PORT} to set up.")

    # ── Build Spotify client (None = disabled gracefully) ──
    spotify = None
    if config.get("client_id") and config.get("client_secret") and config.get("refresh_token"):
        spotify = SpotifyClient(config["client_id"], config["client_secret"], config["refresh_token"])

    # ── Apply the user-entered location from the setup wizard ──
    # effective_location() returns the wizard-chosen city (persisted in the setup
    # file), falling back to the dashboard_config defaults if setup was skipped.
    # This drives weather, AQI, sun times, the flight radius and ISS distance.
    global CHATTANOOGA_LAT, CHATTANOOGA_LON, LOCATION_TZ, LOCATION_NAME
    _geo_city, CHATTANOOGA_LAT, CHATTANOOGA_LON, LOCATION_TZ = _cfg.effective_location()
    LOCATION_NAME = _geo_city
    print(f"Location: {_geo_city} ({CHATTANOOGA_LAT:.4f}, {CHATTANOOGA_LON:.4f}) tz={LOCATION_TZ}")

    # ── HomePod / Apple TV: auto-discover via mDNS ──
    # mDNS scanning allows us to find any HomePod/AppleTV on the local network without hardcoded IPs.
    # This requires: (1) pyatv library installed, (2) mDNS working on the network (port 5353 open)
    # If mDNS fails, music detection from HomePod won't work but the rest of the dashboard continues.
    homepod = None
    homepod_status = "disabled"
    if HAS_PYATV:
        try:
            homepod = HomePodManager()
            print("HomePod/AppleTV auto-discovery started (scanning all devices on network…)")
            homepod_status = "initializing"
        except Exception as e:
            print(f"HomePod init failed (mDNS may be blocked): {e}")
            homepod_status = "init_failed"
    else:
        print("pyatv not installed — HomePod detection disabled")
    flights = FlightTracker()
    weather = WeatherClient()
    aqi_client = AirQualityClient()
    sun_client = SunClient()
    iss = ISSTracker()

    SLOTS = ["clock","weather","sun","photos"]
    slot_idx = 0
    slot_start = time.time()
    last_render = 0
    last_clock_tick = 0

    last_spotify_poll = 0
    _sp_box = [None, False, False]  # [result, has_new_result, is_polling]
    last_flight_poll = 0
    last_iss_poll = 0
    _iss_polling = [False]
    # Weather/AQI/Sun are refreshed off-thread (their fetches can block for
    # several seconds on the open-meteo→wttr.in fallback path). The box holds
    # the latest results; the render paths only ever read the cached copies.
    last_wx_poll = 0
    _wx_box = [None, None, None, False]   # [weather, aqi, sun, is_polling]

    current_spotify = None       # Spotify playing info or None
    current_homepod = None       # HomePod playing info or None
    current_sp_art_path = None   # cached art path for Spotify
    current_hp_art_path = None   # cached art path for HomePod
    last_sp_key = None           # detect Spotify song changes
    last_hp_key = None           # detect HomePod song changes
    in_interrupt = False
    _resume_from_interrupt = False  # set when an interrupt ends, so the resuming
                                    # slide fades back in instead of popping
    _manual_box = [None]           # [plane_dict] for background global fetch
    manual_plane_fetch_time = 0
    manual_search_start = 0
    manual_not_found_until = 0     # show "not found" screen until this time
    last_manual_cs = None
    current_plane_cs = None
    current_plane_show_start = 0
    _current_plane_image = None     # Cache rendered plane so no re-render flicker
    plane_last_shown = {}
    iss_logged = False
    current_photo_path = None
    cached_weather = None
    cached_aqi = None
    cached_sun = None
    last_slide_lock = None   # track changes so we can reset last_render instantly
    last_forced_photo = None
    last_planes = []
    last_status_write = 0
    last_cache_cleanup = time.time()

    cleanup_art_cache()
    # Ensure status file exists and is world-readable for the web server
    try:
        if not os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "w") as _sf:
                _sf.write("{}")
        os.chmod(STATUS_FILE, 0o666)
    except Exception:
        pass
    photo_count = count_photos()
    print("Dashboard started")
    print("Photos in "+str(PHOTOS_DIR)+": "+str(photo_count))
    print("Slots: clock(20s) -> weather(5s) -> sun(5s) -> photos(20s)")
    print("Persistent overlays: flights (while in range), ISS (while overhead), music (while playing)")
    print("Brightness: 6-20=100%, 20-23=35%, 23-6=off")

    try:
        # Boot splash — show immediately before any blocking API calls
        cx, cy = MATRIX_WIDTH // 2, MATRIX_HEIGHT // 2
        boot = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
        bd = ImageDraw.Draw(boot)
        bd.text((cx, cy - 16), "LED",         font=get_font(16), fill=(0, 180, 255),    anchor="mm")
        bd.text((cx, cy -  2), "DASHBOARD",   font=get_font(7),  fill=(60, 80, 120),    anchor="mm")
        bd.text((cx, cy +  9), DASHBOARD_VERSION, font=get_font(6), fill=(30, 50, 80), anchor="mm")
        # "by" label — the animated signature replaces the static credit text

        def _draw_boot_progress(fraction):
            frame = boot.copy()
            fd = ImageDraw.Draw(frame)
            fill_w = int(fraction * MATRIX_WIDTH)
            fd.rectangle([(0, MATRIX_HEIGHT-3),(MATRIX_WIDTH-1, MATRIX_HEIGHT-1)], fill=(0,25,60))
            if fill_w > 0:
                fd.rectangle([(0, MATRIX_HEIGHT-3),(fill_w-1, MATRIX_HEIGHT-1)], fill=(0,120,255))
            display_pil_image(frame)

        _draw_boot_progress(0.0)

        # ── Animated signature: "Kaden" drawn stroke by stroke then a ★ ──
        # The name is defined as a list of polyline strokes in a small
        # coordinate space (0–18 wide, 0–10 tall) that gets scaled and
        # centred in the bottom quarter of the screen.
        #
        # Each stroke is a list of (x, y) points; strokes are drawn
        # sequentially, one segment per frame, to simulate handwriting.
        # Each letter occupies its own non-overlapping x-band (≈5px wide,
        # 1px gap) so "Kaden" reads cleanly instead of clumping together:
        #   K 0-4   a 6-10   d 12-16   e 18-22   n 24-28
        SIG_STROKES = [
            # K — vertical bar
            [(0,0),(0,10)],
            # K — upper arm
            [(0,5),(4,0)],
            # K — lower arm
            [(0,5),(4,10)],
            # a — oval body
            [(9,4),(7,3),(6,5),(7,8),(9,8),(10,6),(9,4)],
            # a — right stroke down
            [(10,4),(10,10)],
            # d — oval body
            [(15,4),(13,3),(12,5),(13,8),(15,8),(16,6),(15,4)],
            # d — right tall stroke
            [(16,0),(16,10)],
            # e — horizontal bar + curve
            [(18,5),(22,5),(22,3),(20,2),(18,4),(18,7),(20,9),(22,8)],
            # n — left stroke + hump + right stroke
            [(24,10),(24,4),(26,2),(28,3),(28,10)],
        ]

        # Map stroke coords → screen pixels
        SIG_X0  = cx - 16      # left edge of signature (28-unit span, centred)
        SIG_Y0  = cy + 18      # top of signature
        SIG_SCX = 1.0          # x scale
        SIG_SCY = 1.0          # y scale
        SIG_COL = DASHBOARD_CREDIT_COLOR   # same pink as original credit

        def _sig_pt(x, y):
            return (int(SIG_X0 + x * SIG_SCX), int(SIG_Y0 + y * SIG_SCY))

        # Build full ordered list of (start, end) segments across all strokes
        _sig_segments = []
        for stroke in SIG_STROKES:
            for j in range(len(stroke) - 1):
                _sig_segments.append((stroke[j], stroke[j+1]))

        # Draw stroke by stroke: each frame adds one segment
        _drawn = []   # segments already drawn
        for seg in _sig_segments:
            _drawn.append(seg)
            frame = boot.copy()
            fd    = ImageDraw.Draw(frame)
            # Re-draw all completed segments
            for s0, s1 in _drawn:
                fd.line([_sig_pt(*s0), _sig_pt(*s1)], fill=SIG_COL, width=1)
            # Progress bar stays at 0 during signature
            fd.rectangle([(0, MATRIX_HEIGHT-3),(MATRIX_WIDTH-1, MATRIX_HEIGHT-1)], fill=(0,25,60))
            display_pil_image(frame)
            time.sleep(0.045)

        # ── Animate the ★ appearing after the name ──────────────────────
        # Star centred just to the right of the last stroke (n ends at x=28)
        STAR_CX = _sig_pt(31, 5)[0]
        STAR_CY = _sig_pt(31, 5)[1]
        STAR_R  = 3   # outer radius

        def _draw_star(d, scx, scy, r, fill, scale=1.0):
            """Draw a 5-pointed star centred at (scx, scy) with outer radius r."""
            pts = []
            for k in range(10):
                angle = math.radians(-90 + k * 36)
                radius = r * scale if k % 2 == 0 else r * scale * 0.45
                pts.append((scx + radius * math.cos(angle),
                             scy + radius * math.sin(angle)))
            d.polygon(pts, fill=fill)

        STAR_COL = (255, 220, 60)   # gold star

        # Star grows from nothing to full size over 12 frames
        for i in range(13):
            scale = i / 12.0
            frame = boot.copy()
            fd    = ImageDraw.Draw(frame)
            # Redraw full signature
            for s0, s1 in _sig_segments:
                fd.line([_sig_pt(*s0), _sig_pt(*s1)], fill=SIG_COL, width=1)
            # Grow star
            if scale > 0:
                _draw_star(fd, STAR_CX, STAR_CY, STAR_R, STAR_COL, scale)
            fd.rectangle([(0, MATRIX_HEIGHT-3),(MATRIX_WIDTH-1, MATRIX_HEIGHT-1)], fill=(0,25,60))
            display_pil_image(frame)
            time.sleep(0.040)

        # Hold signature + star for a moment, then continue to progress bar
        time.sleep(0.3)

        # Fetch weather + AQI in background threads — bar reflects actual completion
        weather_box = [None]; aqi_box = [None]; done_flags = [False, False]
        def _fetch_weather():
            try: weather_box[0] = weather.fetch()
            except Exception: pass  # network error on boot — bar still completes
            done_flags[0] = True
        def _fetch_aqi():
            try: aqi_box[0] = aqi_client.fetch()
            except Exception: pass  # network error on boot — bar still completes
            done_flags[1] = True
        threading.Thread(target=_fetch_weather, daemon=True).start()
        threading.Thread(target=_fetch_aqi,     daemon=True).start()

        # Bar jumps to 50% when weather done, 100% when AQI done.
        # A slow time-drift keeps it visibly moving within each step.
        # Hard 12s timeout so a hung fetch never loops forever.
        max_wait = 12.0
        start_t  = time.time()
        while not all(done_flags):
            elapsed = time.time() - start_t
            if elapsed >= max_wait:
                break
            api_done = sum(done_flags)           # 0, 1, or 2
            # Each completed API = one full step (0→0.5→1.0).
            # Within each step, time drifts the bar up to 45% of the step.
            step_size = 1.0 / len(done_flags)    # 0.5
            drift = min(elapsed / max_wait, 1.0) * step_size * 0.45
            progress = min(api_done * step_size + drift, 0.98)
            _draw_boot_progress(progress)
            time.sleep(0.05)

        _draw_boot_progress(1.0)
        time.sleep(0.2)

        cached_weather    = weather_box[0]
        cached_aqi        = aqi_box[0]
        _wx_box[0]        = cached_weather   # seed the background-refresh box
        _wx_box[1]        = cached_aqi

        try:
            first_slide = render_clock()
            do_globe_intro(first_slide, flights_tracker=flights,
                           city_name=_geo_city.split(",")[0].strip())
        except Exception as e:
            print(f"Startup transition error: {e}")

        # ════════════════════════════════════════════════════════════════════════════════
        # MAIN EVENT LOOP — Display content and handle real-time data updates
        # ════════════════════════════════════════════════════════════════════════════════
        # This loop runs continuously and:
        #   1. Rotates through slides (clock → weather → sun → photos) on a timer
        #   2. Shows persistent overlays when conditions are met (flights when overhead,
        #      ISS when in range, music when playing)
        #   3. Polls APIs in background threads (flights, weather, ISS, HomePod)
        #   4. Handles user overrides (manual callsign search, slide locks, brightness)
        #   5. Updates a status file every 5s for the web dashboard to read
        #
        # Display priority (top to bottom):
        #   - Slide lock (if user locked to a specific slide, only show that)
        #   - Flight overlay (plane in range + manual search take priority)
        #   - ISS overlay (when overhead)
        #   - Music overlay (current Spotify or HomePod track)
        #   - Base slides (clock, weather, sun, photos on rotation)
        #
        # Each slide rotates every SLOT_DURATION seconds (clock 20s, weather 5s, sun 5s, photos 20s)
        # ════════════════════════════════════════════════════════════════════════════════

        while True:
            try:
                now = time.time()

                # ─────────────────────────────────────────────────────────────────────────
                # Every 5s, write a status file for the web server to read
                # This includes current music, flights in range, ISS state, weather, etc.
                # ─────────────────────────────────────────────────────────────────────────
                if now - last_status_write >= 5:
                    last_status_write = now
                    _write_status(current_spotify or current_homepod, last_planes, flights, iss, cached_weather)

                if now - last_cache_cleanup >= 3600:
                    last_cache_cleanup = now
                    cleanup_art_cache()

                # ─────────────────────────────────────────────────────────────────────────
                # Check for user overrides from the web dashboard
                # Overrides include: slide lock (force a specific slide), manual callsign search,
                # forced photo display, brightness override, and disabled slide categories
                # ─────────────────────────────────────────────────────────────────────────
                ov = get_override()
                disabled_slots = set(ov.get("disabled_slides") or [])
                slide_lock = ov.get("slide_lock")
                forced_photo = ov.get("forced_photo")
                if slide_lock != last_slide_lock:
                    last_slide_lock = slide_lock
                    last_render = 0
                    last_clock_tick = 0
                    current_photo_path = None
                    if not slide_lock and forced_photo:
                        slot_idx = SLOTS.index("photos") if "photos" in SLOTS else slot_idx
                        slot_start = now
                if forced_photo != last_forced_photo:
                    last_forced_photo = forced_photo
                    current_photo_path = None
                    last_render = 0
                    if forced_photo and not slide_lock:
                        slot_idx = SLOTS.index("photos") if "photos" in SLOTS else slot_idx
                        slot_start = now

                if slide_lock:
                    anim_frame = int(now * 2)
                    cs_slot = slide_lock
                    if slide_lock == "photos" and now - slot_start >= 20:
                        slot_start = now
                        current_photo_path = None
                        last_render = 0

                    if cs_slot == "clock":
                        if clock_is_single():
                            last_clock_tick = now; last_render = now
                            animate_clock_frames()
                            continue   # smooth bar; skip the shared 0.5s sleep below
                        if now - last_clock_tick >= CLOCK_REFRESH_INTERVAL:
                            display_pil_image(render_clock())
                            last_clock_tick = now; last_render = now

                    elif cs_slot == "weather":
                        # Data kept fresh by the background _poll_wx thread — just render.
                        display_pil_image(render_weather(cached_weather, cached_aqi, anim_frame))
                        last_render = now

                    elif cs_slot == "sun":
                        if last_render == 0 or now - last_render >= 30:
                            display_pil_image(render_sun(cached_sun, cached_weather))
                            last_render = now

                    elif cs_slot == "photos":
                        if current_photo_path is None:
                            photo = pick_photo(last_forced_photo)
                            if photo:
                                current_photo_path = photo
                                last_render = 0
                        if current_photo_path and last_render == 0:
                            try:
                                _ps = _load_photo_settings()
                                _pn = os.path.basename(current_photo_path)
                                display_pil_image(zoom_to_fill_64(Image.open(current_photo_path), _ps.get(_pn)), photo=True)
                            except Exception:
                                current_photo_path = None
                            last_render = now

                    elif cs_slot == "flights":
                        # ─────────────────────────────────────────────────────────────────
                        # FLIGHTS SLIDE: Show aircraft within 25mi of location
                        # Every FLIGHT_POLL_INTERVAL seconds (typically 10-30s), we query 3
                        # flight tracking APIs in parallel (adsblol, airplanes.live, adsbfi)
                        # in a background thread so the display doesn't freeze.
                        # ─────────────────────────────────────────────────────────────────
                        if now - last_flight_poll >= FLIGHT_POLL_INTERVAL:
                            last_flight_poll = now
                            flights.start_poll()  # Spawn background thread with 3 parallel API requests

                        # Get interpolated flight list (positions updated smoothly between API calls)
                        planes = flights.get_interpolated_planes()
                        last_planes = planes  # Cache for status file

                        # For each new plane, fetch its route (origin/dest) in background so we
                        # have full details before showing it
                        for p in planes:
                            _cs = p["callsign"]
                            if _cs not in flights.route_cache and _cs not in flights._route_pending:
                                flights._route_pending.add(_cs)
                                threading.Thread(target=flights.get_route_info, args=(_cs,), daemon=True).start()

                        if planes:
                            # Rotate through planes every 6 seconds (shows each plane at least once)
                            idx = int(now / 6) % len(planes)
                            p = planes[idx]
                            _cs = p["callsign"]
                            route = flights.route_cache.get(_cs) or {"origin":None,"dest":None,"airline_icao":None,"airline_name":None,"expiry":0}
                            display_pil_image(render_flight_image(p, route))
                        else:
                            # No flights in range — show "NO FLIGHTS" screen
                            img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
                            d2 = ImageDraw.Draw(img)
                            d2.text((32, 22), "NO", font=get_font(16), fill=(55, 55, 75), anchor="mm")
                            d2.text((32, 40), "FLIGHTS", font=get_font(10), fill=(45, 45, 65), anchor="mm")
                            display_pil_image(img)
                        last_render = now

                    elif cs_slot == "iss":
                        # ─────────────────────────────────────────────────────────────────
                        # ISS SLIDE: Show International Space Station state
                        # Every ISS_POLL_INTERVAL seconds (typically 10-30s), we query the
                        # open-notify.org API in a background thread to get current ISS position
                        # and calculate distance from our location.
                        # ─────────────────────────────────────────────────────────────────
                        if now - last_iss_poll >= ISS_POLL_INTERVAL and not _iss_polling[0]:
                            last_iss_poll = now
                            def _poll_iss_lock(flag=_iss_polling):
                                flag[0] = True
                                try: iss.poll()
                                finally: flag[0] = False
                            threading.Thread(target=_poll_iss_lock, daemon=True).start()
                        display_pil_image(render_iss(iss))
                        last_render = now

                    time.sleep(0.5)
                    continue

                # ─────────────────────────────────────────────────────────────────────────
                # BACKGROUND POLLING: Keep all data sources fresh in parallel threads
                # We poll on timers to avoid constant API hammering; each poll runs in a
                # daemon thread so the main loop never blocks on network I/O.
                # ─────────────────────────────────────────────────────────────────────────

                # Kick off new flight API queries if the last poll is stale
                if now - last_flight_poll >= FLIGHT_POLL_INTERVAL:
                    last_flight_poll = now
                    flights.start_poll()  # Runs 3 parallel API requests in background

                # Kick off ISS position query if stale (and we're not already polling)
                if now - last_iss_poll >= ISS_POLL_INTERVAL and not _iss_polling[0]:
                    last_iss_poll = now
                    def _poll_iss(flag=_iss_polling):
                        flag[0] = True  # Set flag so we don't double-query
                        try: iss.poll()
                        finally: flag[0] = False  # Clear flag when done
                    threading.Thread(target=_poll_iss, daemon=True).start()

                # HomePod: Check if any HomePod is currently playing music
                # This is non-blocking (just reads cached state from async HomePodManager)
                if homepod:
                    current_homepod = homepod.get_playing()

                # Spotify: Poll Spotify API for currently-playing track
                # Only poll if: (1) Spotify is auth'd, (2) interval has elapsed, (3) not already polling
                if spotify and now - last_spotify_poll >= SPOTIFY_POLL_INTERVAL and not _sp_box[2]:
                    last_spotify_poll = now
                    def _poll_sp(box=_sp_box):
                        box[2] = True  # Set flag: polling in progress
                        try:
                            box[0] = spotify.get_currently_playing()  # Fetch track info from Spotify
                        except Exception as e:
                            print("Spotify poll error: "+str(e))
                            box[0] = None  # Clear on error so we don't show stale music
                        finally:
                            box[1] = True   # Signal that new data is ready
                            box[2] = False  # Clear flag: polling complete
                    threading.Thread(target=_poll_sp, daemon=True).start()

                # Check if Spotify poll just finished and update current_spotify if it did
                if _sp_box[1]:
                    _sp_box[1] = False  # Reset the "new data" flag
                    current_spotify = _sp_box[0]  # Use the freshly polled result

                # Weather / AQI / Sun: refresh in a daemon thread. The clients cache
                # internally (10-min weather/AQI, per-day sun) and persist to disk, so
                # this only touches the network occasionally — but when it does, the
                # open-meteo timeout + wttr.in fallback can take seconds, which must
                # never happen on the display thread. We poll every 60s; the clients
                # decide whether an actual request is needed.
                if now - last_wx_poll >= 60 and not _wx_box[3]:
                    last_wx_poll = now
                    def _poll_wx(box=_wx_box):
                        box[3] = True
                        try:
                            box[0] = weather.fetch()
                            box[1] = aqi_client.fetch()
                            box[2] = sun_client.fetch()
                        except Exception as e:
                            print("Weather/sun poll error: " + str(e))
                        finally:
                            box[3] = False
                    threading.Thread(target=_poll_wx, daemon=True).start()
                # Adopt the freshest background results (never overwrite good data with None)
                if _wx_box[0] is not None: cached_weather = _wx_box[0]
                if _wx_box[1] is not None: cached_aqi     = _wx_box[1]
                if _wx_box[2] is not None: cached_sun     = _wx_box[2]

                # ─────────────────────────────────────────────────────────────────────────
                # PRIORITY 1: FLIGHT INTERRUPTS — Show planes in range, highest priority
                # Flights always interrupt the slide rotation. Each plane shows for
                # PLANE_DISPLAY_DURATION seconds, then waits PLANE_REPEAT_INTERVAL before
                # showing again (to avoid spam).
                # ─────────────────────────────────────────────────────────────────────────
                planes = flights.get_interpolated_planes()
                last_planes = planes  # Cache for status file
                in_range = {p["callsign"] for p in planes}  # Current callsigns in range
                # Clean up cooldown tracking for planes that have left our airspace
                for cs in [k for k in plane_last_shown if k not in in_range]:
                    del plane_last_shown[cs]  # Reset so it can be shown again if it re-enters

                # Kick off background route fetches for any new planes without route info yet
                # (one thread per callsign, with duplicate-prevention)
                for p in planes:
                    cs = p["callsign"]
                    if cs not in flights.route_cache and cs not in flights._route_pending:
                        flights._route_pending.add(cs)  # Mark as "fetch in progress"
                        # Fetch route (origin/dest/airline) in background so the flight display
                        # will be complete when it's shown
                        threading.Thread(target=flights.get_route_info, args=(cs,), daemon=True).start()

                # ─────────────────────────────────────────────────────────────────────────
                # MANUAL FLIGHT SEARCH: User can search for a specific callsign via web UI
                # If found locally, shows immediately; if not in range, searches globally
                # via OpenSky API and shows it if found.
                # ─────────────────────────────────────────────────────────────────────────
                manual_cs = get_manual_track_callsign()  # Read callsign from web override file

                # Show "not found" screen for 4s after timeout, then fully clear
                if manual_not_found_until > 0:
                    if now < manual_not_found_until:
                        img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
                        d = ImageDraw.Draw(img)
                        d.rectangle([(0,0),(63,63)], outline=(120,0,0))
                        d.text((32, 14), "NOT", font=get_font(14), fill=(220, 50, 50), anchor="mm")
                        d.text((32, 29), "FOUND", font=get_font(14), fill=(220, 50, 50), anchor="mm")
                        d.text((32, 44), last_manual_cs or "", font=get_font(8), fill=(160, 160, 160), anchor="mm")
                        d.text((32, 56), "resuming...", font=get_font(6), fill=(80, 80, 80), anchor="mm")
                        display_pil_image(img)
                        in_interrupt = True
                        time.sleep(0.5)
                        continue
                    else:
                        manual_not_found_until = 0
                        last_manual_cs = None

                if not manual_cs:
                    # Reset so the same callsign can be re-searched cleanly next time
                    if last_manual_cs is not None:
                        last_manual_cs = None
                        _manual_box[0] = None

                if manual_cs:
                    if manual_cs != last_manual_cs:
                        _manual_box[0] = None
                        manual_plane_fetch_time = 0
                        manual_search_start = now
                        last_manual_cs = manual_cs
                        write_track_status(manual_cs, "searching")

                    # Timeout after 60s — show not-found screen then resume
                    if _manual_box[0] is None and now - manual_search_start >= 60:
                        manual_not_found_until = now + 4
                        write_track_status(None, "not_found", last_manual_cs)
                    else:
                        # Prefer live in-radius data, fall back to global fetch
                        manual_plane = next((p for p in planes if p["callsign"] == manual_cs), None)
                        if not manual_plane:
                            if now - manual_plane_fetch_time >= 10:
                                manual_plane_fetch_time = now
                                def _fetch_manual(cs=manual_cs, box=_manual_box):
                                    box[0] = flights.fetch_by_callsign(cs)
                                threading.Thread(target=_fetch_manual, daemon=True).start()
                            manual_plane = _manual_box[0]
                        if manual_plane:
                            write_track_status(manual_cs, "tracking")
                            if manual_cs not in flights.route_cache and manual_cs not in flights._route_pending:
                                flights._route_pending.add(manual_cs)
                                threading.Thread(target=flights.get_route_info, args=(manual_cs,), daemon=True).start()
                            route = flights.route_cache.get(manual_cs) or {"origin":None,"dest":None,"airline_icao":None,"airline_name":None,"expiry":0}
                            # Cycle: 5s standard card, 5s detail card
                            if int(now / 5) % 2 == 0:
                                display_pil_image(render_flight_image(manual_plane, route))
                            else:
                                display_pil_image(render_flight_detail(manual_plane, route))
                        else:
                            # Searching screen
                            dot_count = int(now * 1.5) % 3 + 1
                            img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
                            d = ImageDraw.Draw(img)
                            # Plane icon (polygon, always works)
                            cx, cy = 32, 16
                            c = (255, 200, 0)
                            d.ellipse([(cx-2, cy-8),(cx+2, cy+8)], fill=c)
                            d.polygon([(cx-12, cy+4),(cx+12, cy+4),(cx+2,cy-1),(cx-2,cy-1)], fill=c)
                            d.polygon([(cx-5,cy+7),(cx+5,cy+7),(cx+2,cy+4),(cx-2,cy+4)], fill=c)
                            # Callsign
                            d.text((32, 32), manual_cs, font=get_font(9), fill=(255, 255, 255), anchor="mm")
                            # Searching label
                            d.text((32, 44), "SEARCHING", font=get_font(6), fill=(150, 120, 0), anchor="mm")
                            # Animated dots
                            dots_str = " " + "●" * dot_count + "○" * (3 - dot_count)
                            d.text((32, 55), dots_str, font=get_font(7), fill=(255, 180, 0), anchor="mm")
                            display_pil_image(img)
                        in_interrupt = True
                        time.sleep(0.5)
                        continue

                valid_planes = []
                for p in planes:
                    cs = p["callsign"]
                    last_finished = plane_last_shown.get(cs, 0)
                    if last_finished != 0 and now - last_finished < PLANE_REPEAT_INTERVAL:
                        continue  # finished showing recently, in cooldown
                    route = flights.route_cache.get(cs) or {"origin":None,"dest":None,"airline_icao":None,"airline_name":None,"expiry":0}
                    ai_check = route.get("airline_icao") or (
                        cs[:3].upper() if len(cs) >= 3 and cs[:3].isalpha() else None)
                    has_route = bool(route.get("origin") or route.get("dest"))
                    if ai_check and has_route:
                        valid_planes.append((p, route))

                if valid_planes and "flights" not in disabled_slots:
                    p, route = valid_planes[0]
                    cs = p["callsign"]
                    if cs != current_plane_cs:
                        # ── NEW PLANE DETECTED ───────────────────────────────────
                        # 1) Render the flight card ONCE and cache it (rendering every
                        #    frame is wasteful and was a source of micro-stutter).
                        # 2) Play the fly-in animation SYNCHRONOUSLY (blocks ~3.2s).
                        #    We deliberately do NOT show the card before the animation,
                        #    so the card never "flashes" in before the plane wipes it on.
                        #    Synchronous = single display writer = zero flicker.
                        current_plane_cs = cs
                        _current_plane_image = render_flight_image(p, route)   # render once
                        print("PLANE: "+cs+" "+str(round(p["distance"],1))+"mi "+str(p["altitude_ft"])+"ft")
                        if route.get("origin") and route.get("dest"):
                            print("  "+route["origin"]+" -> "+route["dest"])
                        try:
                            do_plane_transition(_current_plane_image)          # fly-in intro
                        except Exception as e:
                            print(f"Plane transition error: {e}")
                        # Start the on-screen hold AFTER the animation finishes so the
                        # static card gets its full PLANE_DISPLAY_DURATION of screen time.
                        current_plane_show_start = time.time()
                    if time.time() - current_plane_show_start < PLANE_DISPLAY_DURATION:
                        # Hold the cached card (no re-render → no stutter, no flicker).
                        display_pil_image(_current_plane_image)
                        in_interrupt = True
                        time.sleep(0.5)
                        continue
                    else:
                        plane_last_shown[cs] = now  # mark FINISHED — start cooldown
                        current_plane_cs = None
                else:
                    current_plane_cs = None

                # PRIORITY 2: ISS — show while overhead (requires fresh data)
                if iss.distance is not None and iss.distance < ISS_OVERHEAD_RADIUS_MILES and iss.is_fresh() and "iss" not in disabled_slots:
                    if not iss_logged:
                        iss_logged = True
                        print("ISS OVERHEAD: "+str(int(iss.distance))+"mi")
                    display_pil_image(render_iss(iss))
                    in_interrupt = True
                    time.sleep(0.5)
                    continue
                else:
                    iss_logged = False

                # Track art paths — update when song changes
                if current_spotify:
                    sk = current_spotify.get("track","").strip() + "|" + current_spotify.get("artist","").strip()
                    if sk != last_sp_key:
                        last_sp_key = sk
                        print("SP: "+current_spotify.get("track","?")+" - "+current_spotify.get("artist","?"))
                        au = current_spotify.get("image_url")
                        current_sp_art_path = download_art(au, "spotify") if au else None
                else:
                    last_sp_key = None; current_sp_art_path = None

                if current_homepod:
                    hk = current_homepod.get("track","").strip() + "|" + current_homepod.get("artist","").strip()
                    if hk != last_hp_key:
                        last_hp_key = hk
                        print("HP: "+current_homepod.get("track","?")+" - "+current_homepod.get("artist","?"))
                        au = current_homepod.get("image_url")
                        if au:
                            current_hp_art_path = au if current_homepod.get("is_local_file") else download_art(au, "homepod")
                        else:
                            current_hp_art_path = None
                else:
                    last_hp_key = None; current_hp_art_path = None

                # PRIORITY 3: MUSIC — show album art while playing
                if current_spotify and current_homepod:
                    # Both playing — split view with labels
                    display_pil_image(render_dual_music(
                        current_spotify, current_sp_art_path,
                        current_homepod, current_hp_art_path))
                    in_interrupt = True
                    time.sleep(0.5)
                    continue
                elif current_spotify or current_homepod:
                    music = current_spotify if current_spotify else current_homepod
                    art_path = current_sp_art_path if current_spotify else current_hp_art_path
                    # Display music — either artwork or the music info even if no artwork
                    if art_path:
                        display_image_file(art_path)
                    else:
                        # No artwork — still show the music source indicator
                        source = "SP" if current_spotify else "HP"
                        img = Image.new("RGB", (MATRIX_WIDTH, MATRIX_HEIGHT), (0, 0, 0))
                        d = ImageDraw.Draw(img)
                        d.text((32, 32), source, font=get_font(20), fill=(100, 100, 255), anchor="mm")
                        display_pil_image(img)
                    in_interrupt = True
                    time.sleep(0.5)
                    continue

                # RETURNING FROM INTERRUPT — reset slot so it re-renders cleanly
                if in_interrupt:
                    in_interrupt = False
                    last_render = 0
                    last_clock_tick = 0
                    _resume_from_interrupt = True   # fade the resuming slide back in

                anim_frame = int(now * 2)  # 2 ticks/sec, time-based so interrupts don't slow it

                # NORMAL SLOT CYCLE
                active_slots = [s for s in SLOTS if s not in disabled_slots]

                # All slides disabled — turn display off
                if not active_slots:
                    _send_raw(Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0)))
                    time.sleep(0.5)
                    continue

                if SLOTS[slot_idx] in disabled_slots:
                    slot_idx = next((i for i, s in enumerate(SLOTS) if s not in disabled_slots), 0)
                    slot_start = now; last_render = 0; last_clock_tick = 0; current_photo_path = None
                cur_slot_dur = SLOT_DURATIONS.get(SLOTS[slot_idx], SLOT_DURATION)
                if now - slot_start >= cur_slot_dur:
                    cur_pos   = active_slots.index(SLOTS[slot_idx]) if SLOTS[slot_idx] in active_slots else 0
                    next_slot = active_slots[(cur_pos + 1) % len(active_slots)]
                    # Pre-render the incoming slide so the fade ALWAYS goes
                    # slide→slide (never slide→black→pop). Photos are picked and
                    # loaded here too so they cross-fade in like every other slide.
                    _next_img = None
                    _next_photo_path = None
                    try:
                        if next_slot == "clock":
                            _next_img = render_clock()
                        elif next_slot == "weather":
                            _next_img = render_weather(cached_weather, cached_aqi, anim_frame)
                        elif next_slot == "sun":
                            _next_img = render_sun(cached_sun, cached_weather)
                        elif next_slot == "photos":
                            _next_photo_path = pick_photo(last_forced_photo)
                            if _next_photo_path:
                                _ps = _load_photo_settings()
                                _next_img = zoom_to_fill_64(
                                    Image.open(_next_photo_path),
                                    _ps.get(os.path.basename(_next_photo_path)))
                    except Exception:
                        _next_img = None
                        _next_photo_path = None
                    do_transition(_next_img)
                    _resume_from_interrupt = False   # the slot-change fade covers it
                    slot_idx   = SLOTS.index(next_slot)
                    slot_start = now
                    last_render = 0
                    last_clock_tick = 0
                    # Carry the pre-picked photo through so the photos branch shows
                    # exactly what we just faded to (no re-pick, no second flash).
                    current_photo_path = _next_photo_path
                cs_slot = SLOTS[slot_idx]

                # Returning from an interrupt (flight/ISS/music) WITHOUT a slot
                # change: fade the resuming slide back in instead of popping it.
                # (If the slot changed above, that fade already ran and cleared
                # this flag.) The plane interrupt has its own animation and is
                # handled before this point, so it is never faded here.
                if _resume_from_interrupt and last_render == 0:
                    _resume_from_interrupt = False
                    try:
                        if cs_slot == "clock":
                            _rimg = render_clock()
                        elif cs_slot == "weather":
                            _rimg = render_weather(cached_weather, cached_aqi, anim_frame)
                        elif cs_slot == "sun":
                            _rimg = render_sun(cached_sun, cached_weather)
                        elif cs_slot == "photos" and current_photo_path:
                            _ps = _load_photo_settings()
                            _rimg = zoom_to_fill_64(
                                Image.open(current_photo_path),
                                _ps.get(os.path.basename(current_photo_path)))
                        else:
                            _rimg = None
                    except Exception:
                        _rimg = None
                    if _rimg is not None:
                        do_transition(_rimg)

                if cs_slot == "clock":
                    if clock_is_single():
                        # Smooth sweeping seconds bar — animate a short batch then
                        # loop back (skips the trailing 0.5s sleep via continue).
                        if last_render == 0: print("CYCLE: clock")
                        last_render = now; last_clock_tick = now
                        animate_clock_frames()
                        continue
                    if now - last_clock_tick >= CLOCK_REFRESH_INTERVAL:
                        if last_render == 0: print("CYCLE: clock")
                        display_pil_image(render_clock())
                        last_clock_tick = now; last_render = now

                elif cs_slot == "weather":
                    # cached_weather/cached_aqi are kept fresh by _poll_wx — render only.
                    if last_render == 0: print("CYCLE: weather")
                    display_pil_image(render_weather(cached_weather, cached_aqi, anim_frame))
                    last_render = now

                elif cs_slot == "sun":
                    if last_render == 0:
                        print("CYCLE: sun")
                        display_pil_image(render_sun(cached_sun, cached_weather))
                        last_render = now
                    elif now - last_render >= 30:
                        display_pil_image(render_sun(cached_sun, cached_weather))
                        last_render = now

                elif cs_slot == "photos":
                    if current_photo_path is None:
                        photo = pick_photo(last_forced_photo)
                        if photo is None:
                            print("CYCLE: photos empty, skipping")
                            cur_pos = active_slots.index("photos") if "photos" in active_slots else 0
                            next_slot = active_slots[(cur_pos + 1) % len(active_slots)]
                            slot_idx = SLOTS.index(next_slot)
                            slot_start = now
                            last_render = 0
                            current_photo_path = None
                            continue
                        current_photo_path = photo
                        print("CYCLE: photo "+os.path.basename(photo)[:30])
                    if last_render == 0:
                        try:
                            _ps = _load_photo_settings()
                            _pn = os.path.basename(current_photo_path)
                            display_pil_image(zoom_to_fill_64(Image.open(current_photo_path), _ps.get(_pn)), photo=True)
                        except Exception as e:
                            print("Photo error: "+str(e))
                            current_photo_path = None
                        last_render = now

                time.sleep(0.5)
            except Exception as _frame_err:
                # A single bad frame (malformed API data, a transient render
                # error, a flaky socket) must never take down the whole display.
                # Log it and keep the loop alive instead of crashing out to systemd
                # and replaying the entire boot/globe-intro sequence.
                import traceback
                print("Main-loop frame error: " + str(_frame_err))
                traceback.print_exc()
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopped")

if __name__ == "__main__":
    main()
# test
