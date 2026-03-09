"""
scanner.py — Descubrimiento de mercados de temperatura máxima en Polymarket
Adaptado de clima-v2: busca mercados en 6 ciudades con YES price 0.03-0.12
"""
import requests
import time
from datetime import datetime, timezone, timedelta

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST  = "https://clob.polymarket.com"

MIN_VOLUME = 200
YES_PRICE_MIN = 0.03
YES_PRICE_MAX = 0.12

# Ciudad → (offset UTC en horas, ventana local inicio, ventana local fin)
# "ventana" = horario local en que el mercado suele estar activo
CITIES = {
    "miami":        {"slug": "miami",         "utc_offset": -5,   "window": (12, 17)},
    "singapore":    {"slug": "singapore",     "utc_offset":  8,   "window": (12, 17)},
    "mumbai":       {"slug": "mumbai",        "utc_offset":  5.5, "window": (12, 17)},
    "cairo":        {"slug": "cairo",         "utc_offset":  2,   "window": (12, 17)},
    "seoul":        {"slug": "seoul",         "utc_offset":  9,   "window": (23, 26)},  # 23-02 crossover
    "buenos-aires": {"slug": "buenos-aires",  "utc_offset": -3,   "window": (12, 17)},
}

MONTH_ABBR = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"]


def _city_local_hour(utc_offset: float) -> float:
    now_utc = datetime.now(timezone.utc)
    local_h = now_utc.hour + now_utc.minute / 60.0 + utc_offset
    return local_h % 24


def _city_is_in_window(utc_offset: float, window: tuple) -> bool:
    local_h = _city_local_hour(utc_offset)
    start, end = window
    if end > 24:  # midnight crossing (e.g. 23-26 → 23-02)
        return local_h >= start or local_h < (end - 24)
    return start <= local_h < end


def _build_slug(city_slug: str, date: datetime) -> str:
    m = MONTH_ABBR[date.month - 1]
    d = date.day
    y = date.year
    return f"highest-temperature-in-{city_slug}-on-{m}-{d}-{y}"


def _fetch_event(slug: str) -> dict | None:
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception:
        return None


def _fetch_yes_price_clob(yes_token_id: str) -> float | None:
    """Obtiene el mejor ask (precio de compra) del order book"""
    try:
        r = requests.get(f"{CLOB_HOST}/book", params={"token_id": yes_token_id}, timeout=6)
        if r.status_code != 200:
            return None
        data = r.json()
        asks = sorted(data.get("asks", []), key=lambda x: float(x["price"]))
        if asks:
            p = float(asks[0]["price"])
            return p if p <= 0.50 else None  # sanity check token invertido
        return None
    except Exception:
        return None


def _hours_into_window(utc_offset: float, window: tuple) -> float:
    local_h = _city_local_hour(utc_offset)
    start = window[0]
    if window[1] > 24:
        if local_h >= start:
            return local_h - start
        else:
            return (24 - start) + local_h
    return max(0.0, local_h - start)


def scan_all_markets(already_tracked: set = None) -> list:
    """
    Escanea todos los mercados de temperatura activos.
    Retorna lista de oportunidades ordenadas por precio YES (menor primero).
    """
    if already_tracked is None:
        already_tracked = set()

    now_utc = datetime.now(timezone.utc)
    opportunities = []

    for city_name, cfg in CITIES.items():
        utc_offset = cfg["utc_offset"]
        window = cfg["window"]

        # Verificar que el mercado esté en horario activo
        if not _city_is_in_window(utc_offset, window):
            continue

        # Intentar hoy y mañana en horario local
        for day_offset in [0, 1]:
            local_date = now_utc + timedelta(hours=utc_offset) + timedelta(days=day_offset)
            slug = _build_slug(cfg["slug"], local_date)

            event = _fetch_event(slug)
            if not event:
                continue

            markets = event.get("markets", [])
            for market in markets:
                # Buscar el outcome YES
                yes_token_id   = market.get("clobTokenIds", [None])[0]
                condition_id   = market.get("conditionId", "")
                yes_price_raw  = market.get("bestAsk") or market.get("outcomePrices", ["0","0"])[0]
                volume_raw     = market.get("volume") or market.get("volume24hr", 0)

                try:
                    yes_price = float(yes_price_raw)
                    volume    = float(volume_raw)
                except (TypeError, ValueError):
                    continue

                if not yes_token_id or not condition_id:
                    continue
                if condition_id in already_tracked:
                    continue
                if volume < MIN_VOLUME:
                    continue
                if not (YES_PRICE_MIN <= yes_price <= YES_PRICE_MAX):
                    continue

                # Obtener precio en tiempo real del CLOB
                clob_price = _fetch_yes_price_clob(yes_token_id)
                if clob_price is not None:
                    yes_price = clob_price

                if not (YES_PRICE_MIN <= yes_price <= YES_PRICE_MAX):
                    continue

                hours_in = _hours_into_window(utc_offset, window)

                opportunities.append({
                    "city":         city_name,
                    "condition_id": condition_id,
                    "yes_token_id": yes_token_id,
                    "yes_price":    round(yes_price, 4),
                    "volume":       round(volume, 0),
                    "hours_in_window": round(hours_in, 1),
                    "question":     market.get("question", slug),
                    "slug":         slug,
                    "scanned_at":   now_utc.isoformat(),
                })

    # Ordenar por precio (menor primero = más upside)
    opportunities.sort(key=lambda x: x["yes_price"])
    return opportunities
