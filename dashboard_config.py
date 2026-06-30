"""
dashboard_config.py — Single source of truth for all site-specific settings.

To deploy on a new machine:
  1. Edit the values in the "CHANGE THESE" sections below.
  2. Leave everything else alone.
  3. Both dashboard.py and dashboard_web.py import this file.
"""

# ─────────────────────────────────────────────────────────────────────────────
# HARDWARE
# ─────────────────────────────────────────────────────────────────────────────
MATRIX_WIDTH   = 64          # LED matrix pixel width
MATRIX_HEIGHT  = 64          # LED matrix pixel height
DISPLAY_BIN    = "/home/kadn/simple_image_display"  # path to the display binary

# ─────────────────────────────────────────────────────────────────────────────
# LOCATION
# ─────────────────────────────────────────────────────────────────────────────
LOCATION_NAME  = "Chattanooga, TN"
LOCATION_LAT   = 35.051815   # decimal degrees, positive = North
LOCATION_LON   = -85.322382  # decimal degrees, negative = West
LOCATION_TZ    = "America/New_York"

# Direction the matrix faces (degrees clockwise from north).
# 0 = matrix faces north, 90 = faces east, 180 = faces south, 270 = faces west.
# Used to orient the heading arrow on the flight-tracker mini radar.
USER_FACING_DEG = 0

# ─────────────────────────────────────────────────────────────────────────────
# WEB DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
WEB_PORT     = 8080
WEB_PASSWORD = "KadenHi5!"
WEB_TITLE    = "LED Dashboard"

# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY IDENTITY  (shown on boot splash)
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_VERSION  = "v2.0"
DASHBOARD_CREDIT   = "by Kaden ♥"
DASHBOARD_CREDIT_COLOR = (255, 80, 160)   # RGB

# ─────────────────────────────────────────────────────────────────────────────
# CLOCK SLIDE — up to 4 timezones shown in a 2×2 grid
# Each entry: (label, IANA_timezone, (R, G, B) label colour)
# ─────────────────────────────────────────────────────────────────────────────
CLOCK_TIMEZONES = [
    ("EST", "America/New_York",  (255, 255, 100)),
    ("CST", "America/Chicago",   (200, 255, 200)),
    ("UTC", "UTC",               (180, 220, 255)),
    ("LON", "Europe/London",     (255, 150, 200)),
]

# ─────────────────────────────────────────────────────────────────────────────
# FLIGHT TRACKER
# ─────────────────────────────────────────────────────────────────────────────
FLIGHT_RADIUS_MILES        = 15    # ADS-B search radius
FLIGHT_POLL_INTERVAL       = 5     # seconds between ADS-B polls
PLANE_DISPLAY_DURATION     = 8     # seconds a plane card stays on screen
PLANE_REPEAT_INTERVAL      = 120   # seconds before showing the same plane again

# ─────────────────────────────────────────────────────────────────────────────
# ISS TRACKER
# ─────────────────────────────────────────────────────────────────────────────
ISS_POLL_INTERVAL          = 30    # seconds between ISS position polls
ISS_OVERHEAD_RADIUS_MILES  = 800   # distance at which ISS is considered "overhead"

# ─────────────────────────────────────────────────────────────────────────────
# SLIDE SHOW
# ─────────────────────────────────────────────────────────────────────────────
SLOT_DURATION          = 20   # default seconds per slide
SLOT_DURATIONS         = {"clock": 20, "weather": 12, "sun": 10, "photos": 20}
PHOTO_ROTATE_INTERVAL  = 5    # seconds between photos in the photos slide
INTERRUPT_DURATION     = 7    # seconds a flight/ISS interrupt card shows

# ─────────────────────────────────────────────────────────────────────────────
# POLLING INTERVALS (seconds)
# ─────────────────────────────────────────────────────────────────────────────
WEATHER_POLL_INTERVAL  = 600
CLOCK_REFRESH_INTERVAL = 1
SPOTIFY_POLL_INTERVAL  = 5
HOMEPOD_POLL_INTERVAL  = 3

# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT BRIGHTNESS SCHEDULE
# Each period ends at the given hour (0-23, supports .5 for :30).
# Night wraps: evening_end → night_end (crosses midnight).
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_BRIGHT_SCHEDULE = {
    "night_bright":   0,   "night_end":    6,
    "morning_bright": 35,  "morning_end":  10,
    "day_bright":     100, "day_end":      20,
    "evening_bright": 35,  "evening_end":  23,
}

# ─────────────────────────────────────────────────────────────────────────────
# FILE PATHS  (all relative to the user's home unless absolute)
# ─────────────────────────────────────────────────────────────────────────────
import os as _os
_HOME = _os.path.expanduser("~")

PHOTOS_DIR           = _os.path.join(_HOME, "dashboard_photos")
CACHE_DIR            = _os.path.join(_HOME, ".dashboard_cache")
MANUAL_TRACK_FILE    = _os.path.join(_HOME, "manual_track.json")
OVERRIDE_FILE        = _os.path.join(_HOME, "dashboard_override.json")
STATUS_FILE          = _os.path.join(_HOME, "dashboard_status.json")
PHOTO_SETTINGS_FILE  = _os.path.join(_HOME, "dashboard_photo_settings.json")
BRIGHT_SCHEDULE_FILE = _os.path.join(_HOME, "dashboard_bright_schedule.json")
CLOCK_SETTINGS_FILE  = _os.path.join(_HOME, "dashboard_clock_settings.json")
PID_FILE             = "/tmp/dashboard.pid"

# ─────────────────────────────────────────────────────────────────────────────
# FIRST-RUN SETUP STATE
# ─────────────────────────────────────────────────────────────────────────────
# Written by the web setup wizard, read by both dashboard.py and dashboard_web.py.
# Persists the user-entered location / web password / Spotify decision so the
# one-time QR setup screen is shown exactly once, then never again.
#
# Shape:
#   {
#     "setup_complete": true,
#     "location": {"name": "...", "lat": 0.0, "lon": 0.0, "tz": "America/..."},
#     "web_password": "...",          # overrides WEB_PASSWORD when present
#     "facing_deg": 0,                # optional matrix orientation
#     "spotify_skipped": false
#   }
SETUP_FILE = _os.path.join(_HOME, ".dashboard_setup.json")


def load_setup():
    """Return the saved setup dict, or {} if no setup has been done yet."""
    import json as _json
    try:
        with open(SETUP_FILE) as _f:
            d = _json.load(_f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_setup(data):
    """Atomically merge *data* into the setup file (partial updates are fine)."""
    import json as _json, tempfile
    current = load_setup()
    current.update(data or {})
    d = _os.path.dirname(SETUP_FILE) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".setup_", suffix=".tmp")
    try:
        with _os.fdopen(fd, "w") as _f:
            _json.dump(current, _f, indent=2)
        _os.replace(tmp, SETUP_FILE)
    except Exception:
        try:
            _os.unlink(tmp)
        except Exception:
            pass
        raise
    return current


def setup_is_complete():
    """True once the user has finished the first-run wizard.

    Legacy migration: installs that predate the wizard won't have a setup file,
    but if Spotify is already authorised they are clearly configured — treat them
    as complete so the one-time QR never appears on an existing display.
    """
    if load_setup().get("setup_complete"):
        return True
    try:
        import json as _json
        with open(_os.path.join(_HOME, ".spotify_display.conf")) as _f:
            if _json.load(_f).get("refresh_token"):
                return True
    except Exception:
        pass
    return False


def effective_location():
    """User-entered location from the wizard, falling back to the config defaults.
    Returns (name, lat, lon, tz)."""
    loc = (load_setup().get("location") or {})
    try:
        lat = float(loc["lat"]); lon = float(loc["lon"])
        name = str(loc.get("name") or LOCATION_NAME)
        tz   = str(loc.get("tz") or LOCATION_TZ)
        return name, lat, lon, tz
    except Exception:
        return LOCATION_NAME, LOCATION_LAT, LOCATION_LON, LOCATION_TZ


def effective_web_password():
    """Wizard-set dashboard password, falling back to the hardcoded default."""
    pw = load_setup().get("web_password")
    return pw if pw else WEB_PASSWORD


def effective_facing_deg():
    """Wizard-set matrix orientation, falling back to the config default."""
    try:
        return int(load_setup().get("facing_deg", USER_FACING_DEG))
    except Exception:
        return USER_FACING_DEG
