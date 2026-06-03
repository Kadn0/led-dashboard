#test webhook
#!/usr/bin/env python3
import os, sys, time, requests, base64, hashlib, json, subprocess, math, threading, asyncio, random, socket
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from pathlib import Path

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
    MANUAL_TRACK_FILE, STATUS_FILE, BRIGHT_SCHEDULE_FILE, PID_FILE,
    PHOTO_SETTINGS_FILE as _PHOTO_SETTINGS_FILE_STR,
)

_PID_FILE = PID_FILE
def _acquire_pid_lock():
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            os.kill(old_pid, 0)
            print(f"Another dashboard instance (pid {old_pid}) is running. Killing it.")
            os.kill(old_pid, 15)
            time.sleep(1)
        except (ProcessLookupError, ValueError):
            pass
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

def get_bright_schedule():
    """Returns {"segments": [{bright, end?}, ...]} — last entry has no 'end' (→ 24:00).
    Reads the new flexible format, or converts the legacy 4-key format on the fly."""
    try:
        if os.path.exists(BRIGHT_SCHEDULE_FILE):
            data = json.loads(open(BRIGHT_SCHEDULE_FILE).read())
            if isinstance(data.get("segments"), list) and data["segments"]:
                return data
            # Convert legacy 4-period format
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

def get_manual_track_callsign():
    try:
        if os.path.exists(MANUAL_TRACK_FILE):
            d = json.loads(open(MANUAL_TRACK_FILE).read())
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

for miss in LOGO_CACHE_DIR.glob("*.miss"):
    try: miss.unlink()
    except: pass

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
                "lat": round(iss_tracker.lat, 2) if iss_tracker.lat else None,
                "lon": round(iss_tracker.lon, 2) if iss_tracker.lon else None,
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
            except: pass
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

CLOCK_TIMEZONES = [
    ("EST","America/New_York",(255,255,100)),
    ("CST","America/Chicago",(200,255,200)),
    ("UTC","UTC",(180,220,255)),
    ("LON","Europe/London",(255,150,200)),
]

WEATHER_LABELS = {
    0:"Clear",1:"Clear",2:"PtCloud",3:"Cloudy",45:"Fog",48:"Fog",
    51:"Drizzle",53:"Drizzle",55:"Drizzle",61:"Rain",63:"Rain",65:"HvyRain",
    71:"Snow",73:"Snow",75:"HvySnow",77:"Snow",80:"Shwr",81:"Shwr",82:"HvyRain",
    85:"Snow",86:"Snow",95:"Storm",96:"Storm",99:"Storm",
}

OVERRIDE_FILE = "/home/kadn/dashboard_override.json"

_override_cache = {}
_override_cache_time = 0.0

def get_override():
    global _override_cache, _override_cache_time
    if time.time() - _override_cache_time < 0.25:
        return _override_cache
    try:
        if os.path.exists(OVERRIDE_FILE):
            _override_cache = json.loads(open(OVERRIDE_FILE).read())
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
    except:
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
        try: return Image.open(cf)
        except: pass

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
            r = requests.get(url, timeout=4)
            if r.status_code != 200 or len(r.content) < 300:
                continue
            img = Image.open(BytesIO(r.content))
            # gstatic returns a tiny 717-byte blank L/LA placeholder for missing logos
            if img.mode in ("L", "LA") and len(r.content) < 1000:
                continue
            img.save(cf, "PNG")
            return Image.open(cf)
        except Exception:
            pass

    miss.touch()
    return None

# ========== MUSIC CLIENTS ==========

# pyatv device model names that can play media (HomePods + Apple TVs)
_HOMEPOD_MODELS = {"HomePod", "HomePodMini", "HomePodGen2"}
_ATV_MODELS     = {"Gen2", "Gen3", "Gen4", "Gen4K",
                   "AppleTV4KGen2", "AppleTV4KGen3", "AppleTVGen1"}
_MEDIA_DEVICE_MODELS = _HOMEPOD_MODELS | _ATV_MODELS | {"Music"}

class HomePodManager:
    """Auto-discovers all HomePods and Apple TVs on the local network via mDNS.
    No static device IDs needed — just start it and it finds whatever is there."""

    RESCAN_INTERVAL = 60   # seconds between full network re-scans

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._conns = {}    # identifier -> live pyatv connection
        self._cfgs  = {}    # identifier -> pyatv config object (from scan)
        self._result = None
        self._lock   = threading.Lock()
        self._running = True
        self._last_scan = 0.0
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        self._loop.run_until_complete(self._poll_loop())

    async def _scan(self):
        """Broadcast mDNS scan — discovers all Apple media devices, no IDs needed."""
        try:
            found = await asyncio.wait_for(pyatv.scan(self._loop), timeout=12)
            for device in found:
                ident = device.identifier
                if ident in self._cfgs:
                    continue
                # Determine model name
                model_name = ""
                try:
                    if device.device_info and device.device_info.model:
                        model_name = device.device_info.model.name
                except Exception:
                    pass
                if model_name not in _MEDIA_DEVICE_MODELS:
                    continue
                self._cfgs[ident] = device
                dtype = "appletv" if model_name in _ATV_MODELS else "homepod"
                print(f"Discovered {dtype}: {device.name} ({model_name}) [{ident}]")
        except Exception as e:
            print(f"HomePod scan: {e}")
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
                self._conns[ident] = await asyncio.wait_for(
                    pyatv.connect(self._cfgs[ident], self._loop), timeout=5)
            except Exception as e:
                print(f"Device connect {ident}: {e}")
                return None
        return self._conns[ident]

    async def _poll_one(self, ident):
        try:
            conn = await self._get_conn(ident)
            if conn is None:
                return None
            info = await asyncio.wait_for(conn.metadata.playing(), timeout=3)
            state = str(info.device_state)
            if "Playing" not in state or not info.title:
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
        except Exception as e:
            print(f"Device poll {ident}: {e}")
            self._conns.pop(ident, None)
            return None

    async def _poll_loop(self):
        await self._scan()
        while self._running:
            # Periodically re-scan to pick up newly appeared devices
            if time.time() - self._last_scan > self.RESCAN_INTERVAL:
                await self._scan()

            idents = list(self._cfgs.keys())
            if idents:
                results = await asyncio.gather(
                    *[self._poll_one(i) for i in idents],
                    return_exceptions=True)
            else:
                results = []
            playing = next((r for r in results if isinstance(r, dict) and r), None)
            if playing:
                ap = None
                if playing["artwork"]:
                    ah = hashlib.md5(playing["artwork"]).hexdigest()
                    af = CACHE_DIR / (playing["dtype"]+"_"+ah+".jpg")
                    if not af.exists():
                        try: Image.open(BytesIO(playing["artwork"])).save(af, "JPEG")
                        except: pass
                    if af.exists(): ap = str(af)
                final = {"track": playing["title"], "artist": playing["artist"],
                         "image_url": ap, "source": playing["dtype"], "is_local_file": True}
            else:
                final = None
            with self._lock:
                self._result = final
            await asyncio.sleep(HOMEPOD_POLL_INTERVAL)

    def get_playing(self):
        with self._lock:
            return self._result

class SpotifyClient:
    def __init__(self, cid, csec, rtok):
        self.cid = cid; self.csec = csec; self.rtok = rtok
        self.access_token = None; self.token_expiry = 0
        self.backoff_until = 0

    def refresh_access_token(self):
        auth = base64.b64encode((self.cid+":"+self.csec).encode()).decode()
        try:
            r = requests.post("https://accounts.spotify.com/api/token",
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
            r = requests.get("https://api.spotify.com/v1/me/player/currently-playing",
                headers={"Authorization":"Bearer "+self.access_token}, timeout=4)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", "30"))
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
    def __init__(self):
        self.cache = None; self.cache_time = 0
    def fetch(self):
        if self.cache and time.time() - self.cache_time < WEATHER_POLL_INTERVAL:
            return self.cache
        try:
            url = ("https://api.open-meteo.com/v1/forecast?latitude="+str(CHATTANOOGA_LAT)+
                   "&longitude="+str(CHATTANOOGA_LON)+
                   "&current=temperature_2m,apparent_temperature,weather_code,uv_index,relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation"+
                   "&daily=temperature_2m_max,temperature_2m_min,weather_code"+
                   "&hourly=uv_index"+
                   "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone="+LOCATION_TZ+"&forecast_days=3")
            r = requests.get(url, timeout=5); r.raise_for_status()
            j = r.json()
            cur = j.get("current",{}); daily = j.get("daily",{}); hourly = j.get("hourly",{})
            day_names = ["MON","TUE","WED","THU","FRI","SAT","SUN"]
            ta = daily.get("time",[]); ha = daily.get("temperature_2m_max",[])
            la = daily.get("temperature_2m_min",[]); ca = daily.get("weather_code",[])
            days = []
            for i in range(min(3, len(ta))):
                try:
                    dt = datetime.fromisoformat(ta[i])
                    lbl = "Now" if i == 0 else day_names[dt.weekday()]
                except: lbl = "?"
                days.append({"label":lbl,"high":ha[i] if i<len(ha) else None,
                            "low":la[i] if i<len(la) else None,
                            "code":ca[i] if i<len(ca) else None})
            self.cache = {"current_temp":cur.get("temperature_2m"),
                         "current_feels":cur.get("apparent_temperature"),
                         "current_code":cur.get("weather_code"),
                         "current_uv":cur.get("uv_index"),
                         "current_humidity":cur.get("relative_humidity_2m"),
                         "current_wind_speed":cur.get("wind_speed_10m"),
                         "current_wind_dir":cur.get("wind_direction_10m"),
                         "current_precip":cur.get("precipitation"),
                         "days":days,
                         "hourly_uv": hourly.get("uv_index",[])[:24]}
            self.cache_time = time.time()
            print("Weather: "+str(int(self.cache["current_temp"]))+"F UV="+str(self.cache["current_uv"]))
            return self.cache
        except Exception as e:
            print("Weather error: "+str(e)); return self.cache

class AirQualityClient:
    def __init__(self):
        self.cache = None; self.cache_time = 0
    def fetch(self):
        if self.cache and time.time() - self.cache_time < WEATHER_POLL_INTERVAL:
            return self.cache
        try:
            url = ("https://air-quality-api.open-meteo.com/v1/air-quality?latitude="+
                   str(CHATTANOOGA_LAT)+"&longitude="+str(CHATTANOOGA_LON)+
                   "&current=us_aqi,pm2_5,pm10")
            r = requests.get(url, timeout=5); r.raise_for_status()
            cur = r.json().get("current",{})
            self.cache = {"aqi":cur.get("us_aqi"),"pm25":cur.get("pm2_5"),"pm10":cur.get("pm10")}
            self.cache_time = time.time()
            print("AQI: "+str(self.cache["aqi"]))
            return self.cache
        except Exception as e:
            print("AQI error: "+str(e)); return self.cache

class SunClient:
    def __init__(self):
        self.cache = None; self.cache_date = None
    def fetch(self):
        try:
            today = datetime.now(ZoneInfo(LOCATION_TZ)).date()
            if self.cache and self.cache_date == today: return self.cache
            url = ("https://api.open-meteo.com/v1/forecast?latitude="+str(CHATTANOOGA_LAT)+
                   "&longitude="+str(CHATTANOOGA_LON)+
                   "&daily=sunrise,sunset&timezone="+LOCATION_TZ+"&forecast_days=1")
            r = requests.get(url, timeout=5); r.raise_for_status()
            d = r.json().get("daily",{})
            srs = d.get("sunrise") or []; sss = d.get("sunset") or []
            if not srs or not sss: return self.cache
            sr = datetime.fromisoformat(srs[0]); ss = datetime.fromisoformat(sss[0])
            self.cache = {"sunrise":sr,"sunset":ss}
            self.cache_date = today
            print("Sun: "+sr.strftime("%H:%M")+" - "+ss.strftime("%H:%M"))
            return self.cache
        except Exception as e:
            print("Sun error: "+str(e)); return self.cache

# ========== ISS ==========
class ISSTracker:
    def __init__(self):
        self.lat = None; self.lon = None
        self.distance = None; self.last_distance = None
        self.last_poll_time = 0
    def poll(self):
        try:
            r = requests.get("http://api.open-notify.org/iss-now.json", timeout=5)
            r.raise_for_status()
            pos = r.json().get("iss_position",{})
            self.lat = float(pos.get("latitude")); self.lon = float(pos.get("longitude"))
            self.last_distance = self.distance
            self.distance = haversine_miles(CHATTANOOGA_LAT, CHATTANOOGA_LON, self.lat, self.lon)
            self.last_poll_time = time.time()
            return True
        except Exception as e:
            print("ISS error: "+str(e)); return False
    def is_fresh(self):
        return time.time() - self.last_poll_time < 120  # stale after 2 min
    def just_became_overhead(self):
        if self.distance is None: return False
        if self.last_distance is None: return self.distance < ISS_OVERHEAD_RADIUS_MILES
        return self.distance < ISS_OVERHEAD_RADIUS_MILES and self.last_distance >= ISS_OVERHEAD_RADIUS_MILES

# ========== FLIGHTS ==========
class FlightTracker:
    def __init__(self):
        self.route_cache = {}; self.positions = {}
        self.lock = threading.Lock(); self.api_index = 0
        self._route_pending = set()  # callsigns currently being fetched
    def poll_in_background(self):
        apis = [self._poll_adsblol, self._poll_airplaneslive, self._poll_adsbfi]
        planes = []; tries = 0
        while not planes and tries < len(apis):
            idx = (self.api_index + tries) % len(apis)
            planes = apis[idx]()
            tries += 1
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
            r = requests.get("https://api.adsb.lol/v2/lat/"+str(CHATTANOOGA_LAT)+"/lon/"+str(CHATTANOOGA_LON)+"/dist/25", timeout=8)
            r.raise_for_status()
            return self._parse(r.json().get("ac") or [])
        except Exception as e:
            print("adsb.lol err: "+str(e)); return []
    def _poll_airplaneslive(self):
        try:
            r = requests.get("https://api.airplanes.live/v2/point/"+str(CHATTANOOGA_LAT)+"/"+str(CHATTANOOGA_LON)+"/25", timeout=8)
            r.raise_for_status()
            return self._parse(r.json().get("ac") or [])
        except Exception as e:
            print("airplanes.live err: "+str(e)); return []
    def _poll_adsbfi(self):
        try:
            r = requests.get("https://api.adsb.fi/v2/lat/"+str(CHATTANOOGA_LAT)+"/lon/"+str(CHATTANOOGA_LON)+"/dist/25", timeout=8)
            r.raise_for_status()
            return self._parse(r.json().get("ac") or [])
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
                spd = (d["speed_mph"]/3600)/69.0
                hr = math.radians(d["heading"])
                nlat = d["lat"] + spd*dt*math.cos(hr)
                nlon = d["lon"] + spd*dt*math.sin(hr)
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
            r = requests.get("https://api.adsbdb.com/v0/callsign/"+callsign, timeout=3)
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
                r2 = requests.get("https://opensky-network.org/api/routes",
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
        # Prune cache when it gets large — remove expired entries first
        if len(self.route_cache) > 400:
            _now = time.time()
            expired = [k for k, v in self.route_cache.items() if _now > v.get("expiry", 0)]
            for k in expired[:200]:
                del self.route_cache[k]
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
            "https://api.adsb.fi/v2/callsign/"+callsign,
        ]:
            try:
                r = requests.get(url, timeout=5)
                if r.status_code != 200: continue
                ac_list = r.json().get("ac") or []
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
def render_clock():
    img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT),(0,0,0))
    draw = ImageDraw.Draw(img)
    lf = get_font(7); tf = get_font(9); sf = get_font(7)
    for i,(label,tz,color) in enumerate(CLOCK_TIMEZONES):
        col = i%2; row = i//2
        x = col*32; y = row*32
        try:
            dt = datetime.now(ZoneInfo(tz))
            h12 = dt.hour%12 or 12
            ts = str(h12)+":"+("%02d"%dt.minute)
            ss = ":"+("%02d"%dt.second)
        except: ts = "?"; ss = ""
        bbox = draw.textbbox((0,0),label,font=lf); w = bbox[2]-bbox[0]
        draw.text((x+(32-w)//2, y+1), label, font=lf, fill=color)
        bbox = draw.textbbox((0,0),ts,font=tf); w = bbox[2]-bbox[0]
        draw.text((x+(32-w)//2, y+10), ts, font=tf, fill=(255,255,255))
        bbox = draw.textbbox((0,0),ss,font=sf); w = bbox[2]-bbox[0]
        draw.text((x+(32-w)//2, y+22), ss, font=sf, fill=(160,160,160))
    draw.line([(32,0),(32,63)], fill=(40,40,60))
    draw.line([(0,32),(63,32)], fill=(40,40,60))
    return img

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
        draw_small_weather_icon(draw, cx, 37, day.get("code"), frame)
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
        except: return "--:--"

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

        # Peak dot (colored)
        if peak_uv is not None:
            px, py = pts[peak_hour]
            draw.ellipse([(px-2,py-2),(px+2,py+2)], fill=_uv_col(peak_uv))

        # "Now" marker  — vertical dim line + white dot
        if 0 <= now_hour <= 23:
            nx, ny = pts[now_hour]
            draw.line([(nx, GY0), (nx, GY1)], fill=(48, 48, 68))
            draw.ellipse([(nx-2,ny-2),(nx+2,ny+2)], fill=(255,255,255))
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
        # flicker: each star independently on/off based on hash of pos+tick
        flicker_seed = (sx * 97 + sy * 31 + tick) % 7
        if flicker_seed < 5:  # 5/7 chance visible each frame
            sb = rg.randint(120, 255)
            draw.point((sx, sy), fill=(sb, sb, min(sb + 20, 255)))

    # ISS label
    draw.text((32, 5), "ISS", font=get_font(9), fill=(120, 230, 255), anchor="mm")
    draw.text((32, 13), "OVERHEAD", font=get_font(7), fill=(255, 215, 70), anchor="mm")

    # ── ISS silhouette — compact, centered at (32, 24) ──────────────────
    cx, cy = 32, 24
    truss_col  = (170, 175, 180)
    panel_col  = (45, 85, 185)
    cell_col   = (22, 45, 105)
    module_col = (200, 205, 210)

    # Main truss: thin 1-px backbone
    draw.line([(cx - 16, cy), (cx + 16, cy)], fill=truss_col, width=2)

    # Solar arrays: 2 pairs per side, each 5 px wide × 10 px tall
    # Inner pair at ±5 from center, outer at ±13
    for side in (-1, 1):
        for offset in (5, 13):
            px = cx + side * offset
            x0, x1 = px - 2, px + 2
            y0, y1 = cy - 6, cy + 6
            draw.rectangle([(x0, y0), (x1, y1)], fill=panel_col)
            # Cell dividers
            for gy in range(y0 + 3, y1, 3):
                draw.line([(x0, gy), (x1, gy)], fill=cell_col)

    # Central habitation module — compact silver box
    draw.rectangle([(cx - 3, cy - 3), (cx + 3, cy + 3)], fill=module_col)
    draw.line([(cx - 3, cy), (cx + 3, cy)], fill=(150, 155, 160))  # equator seam

    # Small nadir/zenith radiators (perpendicular to truss)
    draw.rectangle([(cx - 1, cy - 7), (cx + 1, cy - 4)], fill=(220, 225, 230))
    draw.rectangle([(cx - 1, cy + 4), (cx + 1, cy + 7)], fill=(220, 225, 230))

    # ── Info — three lines stacked below sprite (sprite bottom = cy+7 = 31) ──
    if iss and iss.distance is not None:
        dist_str = f"{int(iss.distance):,} mi away"
        bbox = draw.textbbox((0, 0), dist_str, font=get_font(7))
        draw.text(((64 - (bbox[2]-bbox[0])) // 2, 33), dist_str, font=get_font(7), fill=(255, 255, 255))
    draw.text((32, 44), "17,500 mph", font=get_font(7), fill=(160, 210, 255), anchor="mm")
    draw.text((32, 54), "~250 mi up", font=get_font(7), fill=(140, 190, 255), anchor="mm")
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
        except: pass
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
                    except: pass
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
        except: return None

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

def get_display_proc():
    global _display_proc
    if _display_proc is None or _display_proc.poll() is not None:
        _display_proc = subprocess.Popen([DISPLAY_BIN], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _display_proc

def _send_raw(img):
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
    if not photo and get_brightness() <= 50:
        img = img.point(lambda p: p if p > 22 else 0)
    _last_raw_img = img.copy()
    _send_raw(apply_dimming(img))

def do_transition():
    global _last_raw_img
    if _last_raw_img is None: return
    b = get_brightness()
    steps = [0.55, 0.32, 0.16, 0.06, 0.0]
    for f in steps:
        _send_raw(ImageEnhance.Brightness(_last_raw_img).enhance(b * f) if f > 0 else Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT),(0,0,0)))
        time.sleep(0.04)
    time.sleep(0.04)

def do_plane_transition(flight_img):
    """Full-screen top-down plane; wings split apart to reveal the flight info slide."""
    b = get_brightness()
    W, H = MATRIX_WIDTH, MATRIX_HEIGHT
    cx = W // 2  # 32

    plane_img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(plane_img)

    body  = (210, 215, 220)   # silver fuselage
    wing  = (155, 165, 178)   # slightly darker wing surfaces
    glass = (70, 155, 255)    # cockpit blue

    # Fuselage — runs nearly full height, 4px wide
    d.rectangle([(cx-2, 5), (cx+2, 59)], fill=body)
    # Nose taper
    d.rectangle([(cx-1, 3), (cx+1, 5)], fill=body)
    d.point((cx, 2), fill=body)
    # Cockpit windows
    d.rectangle([(cx-1, 9), (cx+1, 14)], fill=glass)

    # Main wings — swept, span full 64px width at center (y=28)
    wing_y = 28
    for dy in range(-9, 10):
        y = wing_y + dy
        spread = int((1.0 - abs(dy) / 9.0 * 0.82) * 31)
        d.line([(cx - spread, y), (cx + spread, y)], fill=wing)

    # Tail fins
    for dy in range(-5, 6):
        y = 47 + dy
        spread = int((1.0 - abs(dy) / 5.0 * 0.65) * 14)
        d.line([(cx - spread, y), (cx + spread, y)], fill=wing)

    # Phase 1: hold the plane on screen briefly
    for _ in range(10):
        _send_raw(ImageEnhance.Brightness(plane_img).enhance(b))
        time.sleep(0.04)

    # Phase 2: barn-door wipe — top half slides up, bottom half slides down
    # revealing the flight_img underneath, splitting at the wing centerline
    split = wing_y
    steps = 24
    for step in range(steps + 1):
        offset = int((step / steps) * (H // 2 + 10))
        frame = flight_img.copy()
        # Top portion of plane shifts up by offset
        frame.paste(plane_img.crop((0, 0, W, split)), (0, -offset))
        # Bottom portion of plane shifts down by offset
        frame.paste(plane_img.crop((0, split, W, H)), (0, split + offset))
        _send_raw(ImageEnhance.Brightness(frame).enhance(b))
        time.sleep(0.028)


_file_cache_path = None
_file_cache_img = None
def display_image_file(path):
    global _file_cache_path, _file_cache_img
    try:
        if path != _file_cache_path:
            _file_cache_path = path
            _file_cache_img = Image.open(path).copy()
        display_pil_image(_file_cache_img)
    except Exception as e:
        print("Display error ("+str(path)+"): "+str(e))
        _file_cache_path = None

def download_art(image_url, prefix="art"):
    if not image_url: return None
    ck = hashlib.md5(image_url.encode()).hexdigest()
    cf = CACHE_DIR / (prefix+"_"+ck+".jpg")
    if cf.exists(): return str(cf)
    try:
        r = requests.get(image_url, timeout=3); r.raise_for_status()
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
        r = requests.get(
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

    # ── One-time startup geolocation (overrides config coords if successful) ──
    global CHATTANOOGA_LAT, CHATTANOOGA_LON
    geo = _detect_location()
    if geo:
        CHATTANOOGA_LAT, CHATTANOOGA_LON, _geo_city = geo
        print(f"Location auto-detected: {_geo_city} ({CHATTANOOGA_LAT:.4f}, {CHATTANOOGA_LON:.4f})")
    else:
        print(f"Location: using config ({CHATTANOOGA_LAT:.4f}, {CHATTANOOGA_LON:.4f})")

    # ── HomePod / Apple TV: auto-discover via mDNS, no static IDs needed ──
    homepod = HomePodManager() if HAS_PYATV else None
    if HAS_PYATV:
        print("HomePod/AppleTV auto-discovery started (scanning network…)")
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

    current_spotify = None       # Spotify playing info or None
    current_homepod = None       # HomePod playing info or None
    current_sp_art_path = None   # cached art path for Spotify
    current_hp_art_path = None   # cached art path for HomePod
    last_sp_key = None           # detect Spotify song changes
    last_hp_key = None           # detect HomePod song changes
    in_interrupt = False
    _manual_box = [None]           # [plane_dict] for background global fetch
    manual_plane_fetch_time = 0
    manual_search_start = 0
    manual_not_found_until = 0     # show "not found" screen until this time
    last_manual_cs = None
    current_plane_cs = None
    current_plane_show_start = 0
    plane_last_shown = {}
    iss_logged = False
    current_photo_path = None
    cached_weather = None
    cached_aqi = None
    weather_fetch_time = 0
    last_slide_lock = None   # track changes so we can reset last_render instantly
    last_forced_photo = None
    last_planes = []
    last_status_write = 0

    cleanup_art_cache()
    # Ensure status file exists and is world-readable for the web server
    try:
        if not os.path.exists(STATUS_FILE):
            open(STATUS_FILE, "w").write("{}")
        os.chmod(STATUS_FILE, 0o666)
    except: pass
    photo_count = count_photos()
    print("Dashboard started")
    print("Photos in "+str(PHOTOS_DIR)+": "+str(photo_count))
    print("Slots: clock(20s) -> weather(5s) -> sun(5s) -> photos(20s)")
    print("Persistent overlays: flights (while in range), ISS (while overhead), music (while playing)")
    print("Brightness: 6-20=100%, 20-23=35%, 23-6=off")

    try:
        cached_weather = weather.fetch()
        cached_aqi = aqi_client.fetch()
        weather_fetch_time = time.time()

        # Boot splash
        cx, cy = MATRIX_WIDTH // 2, MATRIX_HEIGHT // 2
        boot = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
        bd = ImageDraw.Draw(boot)
        bd.text((cx, cy - 16), "LED",         font=get_font(16), fill=(0, 180, 255),    anchor="mm")
        bd.text((cx, cy -  2), "DASHBOARD",   font=get_font(7),  fill=(60, 80, 120),    anchor="mm")
        bd.text((cx, cy +  9), DASHBOARD_VERSION, font=get_font(6), fill=(30, 50, 80), anchor="mm")
        bd.text((cx, cy + 22), DASHBOARD_CREDIT,  font=get_font(6), fill=DASHBOARD_CREDIT_COLOR, anchor="mm")
        bd.rectangle([(0, MATRIX_HEIGHT - 3), (MATRIX_WIDTH - 1, MATRIX_HEIGHT - 1)], fill=(0, 80, 160))
        display_pil_image(boot)
        time.sleep(2.0)
        do_plane_transition(Image.new("RGB", (MATRIX_WIDTH, MATRIX_HEIGHT), (0, 0, 0)))

        while True:
            now = time.time()

            # Status file — always update every 5s using last-known data
            if now - last_status_write >= 5:
                last_status_write = now
                _write_status(current_spotify or current_homepod, last_planes, flights, iss, cached_weather)

            # Check slide lock — if set, override everything (flights, music, ISS)
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
                    if now - last_clock_tick >= CLOCK_REFRESH_INTERVAL:
                        display_pil_image(render_clock())
                        last_clock_tick = now; last_render = now

                elif cs_slot == "weather":
                    if last_render == 0 or now - weather_fetch_time >= 30:
                        cached_weather = weather.fetch()
                        cached_aqi = aqi_client.fetch()
                        weather_fetch_time = now
                    display_pil_image(render_weather(cached_weather, cached_aqi, anim_frame))
                    last_render = now

                elif cs_slot == "sun":
                    if last_render == 0 or now - last_render >= 30:
                        display_pil_image(render_sun(sun_client.fetch(), cached_weather))
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
                    # Poll flights so the display stays live
                    if now - last_flight_poll >= FLIGHT_POLL_INTERVAL:
                        last_flight_poll = now
                        flights.start_poll()
                    planes = flights.get_interpolated_planes()
                    last_planes = planes
                    for p in planes:
                        _cs = p["callsign"]
                        if _cs not in flights.route_cache and _cs not in flights._route_pending:
                            flights._route_pending.add(_cs)
                            threading.Thread(target=flights.get_route_info, args=(_cs,), daemon=True).start()
                    if planes:
                        idx = int(now / 6) % len(planes)
                        p = planes[idx]
                        _cs = p["callsign"]
                        route = flights.route_cache.get(_cs) or {"origin":None,"dest":None,"airline_icao":None,"airline_name":None,"expiry":0}
                        display_pil_image(render_flight_image(p, route))
                    else:
                        img = Image.new("RGB",(MATRIX_WIDTH,MATRIX_HEIGHT), (0, 0, 0))
                        d2 = ImageDraw.Draw(img)
                        d2.text((32, 22), "NO", font=get_font(16), fill=(55, 55, 75), anchor="mm")
                        d2.text((32, 40), "FLIGHTS", font=get_font(10), fill=(45, 45, 65), anchor="mm")
                        display_pil_image(img)
                    last_render = now

                elif cs_slot == "iss":
                    # Poll ISS so the display stays live
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

            # POLL DATA SOURCES
            if now - last_flight_poll >= FLIGHT_POLL_INTERVAL:
                last_flight_poll = now
                flights.start_poll()

            if now - last_iss_poll >= ISS_POLL_INTERVAL and not _iss_polling[0]:
                last_iss_poll = now
                def _poll_iss(flag=_iss_polling):
                    flag[0] = True
                    try: iss.poll()
                    finally: flag[0] = False
                threading.Thread(target=_poll_iss, daemon=True).start()

            if homepod:
                current_homepod = homepod.get_playing()

            if spotify and now - last_spotify_poll >= SPOTIFY_POLL_INTERVAL and not _sp_box[2]:
                last_spotify_poll = now
                def _poll_sp(box=_sp_box):
                    box[2] = True
                    try:
                        box[0] = spotify.get_currently_playing()
                    except Exception as e:
                        print("Spotify poll error: "+str(e))
                        box[0] = None   # clear on unexpected error
                    finally:
                        box[1] = True   # always signal a new result so current_spotify updates
                        box[2] = False
                threading.Thread(target=_poll_sp, daemon=True).start()

            if _sp_box[1]:
                _sp_box[1] = False
                current_spotify = _sp_box[0]

            # PRIORITY 1: FLIGHTS — always beats music; repeats after PLANE_REPEAT_INTERVAL
            planes = flights.get_interpolated_planes()
            last_planes = planes
            in_range = {p["callsign"] for p in planes}
            for cs in [k for k in plane_last_shown if k not in in_range]:
                del plane_last_shown[cs]  # reset cooldown when plane leaves radius

            # Kick off background route fetches (one thread per callsign, no duplicates)
            for p in planes:
                cs = p["callsign"]
                if cs not in flights.route_cache and cs not in flights._route_pending:
                    flights._route_pending.add(cs)
                    threading.Thread(target=flights.get_route_info, args=(cs,), daemon=True).start()

            # Manual track: search globally every 10s, show persistently
            manual_cs = get_manual_track_callsign()

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
                    current_plane_cs = cs
                    current_plane_show_start = now
                    # plane_last_shown set AFTER display completes, not on detection
                    print("PLANE: "+cs+" "+str(round(p["distance"],1))+"mi "+str(p["altitude_ft"])+"ft")
                    do_plane_transition(render_flight_image(p, route))
                    if route.get("origin") and route.get("dest"):
                        print("  "+route["origin"]+" -> "+route["dest"])
                if now - current_plane_show_start < PLANE_DISPLAY_DURATION:
                    display_pil_image(render_flight_image(p, route))
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
                sk = (current_spotify.get("track","")) + "|" + (current_spotify.get("artist",""))
                if sk != last_sp_key:
                    last_sp_key = sk
                    print("SP: "+current_spotify.get("track","?")+" - "+current_spotify.get("artist","?"))
                    au = current_spotify.get("image_url")
                    current_sp_art_path = download_art(au, "spotify") if au else None
            else:
                last_sp_key = None; current_sp_art_path = None

            if current_homepod:
                hk = (current_homepod.get("track","")) + "|" + (current_homepod.get("artist",""))
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
                art_path = current_sp_art_path if current_spotify else current_hp_art_path
                if art_path:
                    display_image_file(art_path)
                    in_interrupt = True
                    time.sleep(0.5)
                    continue

            # RETURNING FROM INTERRUPT — reset slot so it re-renders cleanly
            if in_interrupt:
                in_interrupt = False
                last_render = 0
                last_clock_tick = 0

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
                do_transition()
                cur_pos = active_slots.index(SLOTS[slot_idx]) if SLOTS[slot_idx] in active_slots else 0
                next_slot = active_slots[(cur_pos + 1) % len(active_slots)]
                slot_idx = SLOTS.index(next_slot)
                slot_start = now
                last_render = 0
                last_clock_tick = 0
                current_photo_path = None
            cs_slot = SLOTS[slot_idx]

            if cs_slot == "clock":
                if now - last_clock_tick >= CLOCK_REFRESH_INTERVAL:
                    if last_render == 0: print("CYCLE: clock")
                    display_pil_image(render_clock())
                    last_clock_tick = now; last_render = now

            elif cs_slot == "weather":
                if last_render == 0 or now - weather_fetch_time >= 30:
                    if last_render == 0: print("CYCLE: weather")
                    cached_weather = weather.fetch()
                    cached_aqi = aqi_client.fetch()
                    weather_fetch_time = now
                display_pil_image(render_weather(cached_weather, cached_aqi, anim_frame))
                last_render = now

            elif cs_slot == "sun":
                if last_render == 0:
                    print("CYCLE: sun")
                    display_pil_image(render_sun(sun_client.fetch(), cached_weather))
                    last_render = now
                elif now - last_render >= 30:
                    display_pil_image(render_sun(sun_client.fetch(), cached_weather))
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
    except KeyboardInterrupt:
        print("Stopped")

if __name__ == "__main__":
    main()
# test
