"""
config.py — Configuración central de Clima-Gold
100% fiel a clima-v2 (excepto capital real desde Polymarket)
"""
import os

# ── Day-of-week regime ────────────────────────────────────────────────────────
WEEKEND_ENABLED   = os.environ.get("WEEKEND_ENABLED",   "true").lower() == "true"

WEEKDAY_YES_MIN   = float(os.environ.get("WEEKDAY_YES_MIN",   0.06))
WEEKDAY_YES_MAX   = float(os.environ.get("WEEKDAY_YES_MAX",   0.115))
WEEKDAY_MIN_SCORE = int(os.environ.get("WEEKDAY_MIN_SCORE",   60))

WEEKEND_YES_MIN   = float(os.environ.get("WEEKEND_YES_MIN",   0.06))
WEEKEND_YES_MAX   = float(os.environ.get("WEEKEND_YES_MAX",   0.115))
WEEKEND_MIN_SCORE = int(os.environ.get("WEEKEND_MIN_SCORE",   60))

MIN_YES_PRICE   = WEEKDAY_YES_MIN
MAX_YES_PRICE   = WEEKDAY_YES_MAX

# ── Take profit ───────────────────────────────────────────────────────────────
TAKE_PROFIT_YES = float(os.environ.get("TAKE_PROFIT_YES", 0.15))

# ── Volume thresholds para scoring ────────────────────────────────────────────
SCORE_VOLUME_HIGH = float(os.environ.get("SCORE_VOLUME_HIGH", 500))
SCORE_VOLUME_MID  = float(os.environ.get("SCORE_VOLUME_MID",  300))
SCORE_VOLUME_LOW  = float(os.environ.get("SCORE_VOLUME_LOW",  200))

# ── Price history ─────────────────────────────────────────────────────────────
PRICE_HISTORY_TTL = int(os.environ.get("PRICE_HISTORY_TTL", 3600))

# ── Position sizing (2.0%–3.0% inversamente proporcional al YES price) ────────
POSITION_SIZE_MIN = float(os.environ.get("POSITION_SIZE_MIN", 0.020))
POSITION_SIZE_MAX = float(os.environ.get("POSITION_SIZE_MAX", 0.030))

# ── Parámetros de escaneo ─────────────────────────────────────────────────────
MIN_VOLUME            = float(os.environ.get("MIN_VOLUME",             200))
MONITOR_INTERVAL      = int(os.environ.get("MONITOR_INTERVAL",          30))
PRICE_UPDATE_INTERVAL = int(os.environ.get("PRICE_UPDATE_INTERVAL",      5))
SCAN_DAYS_AHEAD       = int(os.environ.get("SCAN_DAYS_AHEAD",            1))
MAX_POSITIONS         = int(os.environ.get("MAX_POSITIONS",             20))

# ── Zona horaria del observador (Chile = UTC-3) ───────────────────────────────
OBSERVER_UTC_OFFSET = int(os.environ.get("OBSERVER_UTC_OFFSET", -3))

# ── Ventanas horarias por ciudad (hora Chile) ─────────────────────────────────
# Formato: (open_h, open_m, close_h, close_m)
# Al cierre se fuerza el cierre de todas las posiciones de esa ciudad.
CITY_WINDOWS = {
    "buenos-aires": (11,  0, 15,  0),
    "london":       ( 8,  0, 10,  0),
    "miami":        (11,  0, 17,  0),
    "paris":        ( 6,  0, 10,  0),
    "toronto":      (14,  0, 17,  0),
    "seattle":      (16,  0, 20,  0),
    "wellington":   (19,  0, 21, 30),
    "sao-paulo":    (11,  0, 13,  0),
    "seoul":        ( 0,  0,  1,  0),
}

# ── Límites de exposición regional ───────────────────────────────────────────
MAX_REGION_EXPOSURE  = float(os.environ.get("MAX_REGION_EXPOSURE",  0.25))
BUY_TIMEOUT_MINUTES  = int(os.environ.get("BUY_TIMEOUT_MINUTES",   2))

REGION_MAP = {
    "buenos-aires": "southern",
    "london":       "europe",
    "miami":        "south",
    "paris":        "europe",
    "seattle":      "pacific",
    "wellington":   "southern",
    "sao-paulo":    "southern",
    "seoul":        "asia",
}

# ── Capital ───────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", 100.0))

# ── API ───────────────────────────────────────────────────────────────────────
GAMMA = os.environ.get("GAMMA_API", "https://gamma-api.polymarket.com")

# ── UTC offsets de cada ciudad (para verificar fecha local correcta) ──────────
CITY_UTC_OFFSET = {
    "buenos-aires": -3,
    "london":        0,
    "miami":        -5,
    "paris":         1,
    "toronto":      -5,
    "seattle":      -8,
    "wellington":   13,
    "sao-paulo":    -3,
    "seoul":         9,
}

# ── Ciudades activas (idénticas a clima-v2) ───────────────────────────────────
WEATHER_CITIES = [
    "buenos-aires",
    "london",
    "miami",
    "paris",
    "toronto",
    "seattle",
    "wellington",
    "sao-paulo",
    "seoul",
]
