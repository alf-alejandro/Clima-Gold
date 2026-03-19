"""
scanner.py — Descubrimiento de mercados de temperatura en Polymarket
100% fiel a clima-v2: 8 ciudades, ventanas hora Chile, meses completos
"""
import json
import logging
import requests
from datetime import datetime, timezone, timedelta

from config import (
    GAMMA, WEATHER_CITIES, MIN_YES_PRICE, MAX_YES_PRICE, TAKE_PROFIT_YES,
    MIN_VOLUME, SCAN_DAYS_AHEAD, CITY_UTC_OFFSET, OBSERVER_UTC_OFFSET,
    CITY_WINDOWS,
)

CLOB = "https://clob.polymarket.com"
log  = logging.getLogger(__name__)

MONTHS = {
    1: "january", 2: "february",  3: "march",    4: "april",
    5: "may",     6: "june",      7: "july",     8: "august",
    9: "september", 10: "october", 11: "november", 12: "december",
}


def now_utc():
    return datetime.now(timezone.utc)


def parse_price(val):
    try:
        return float(val)
    except Exception:
        return None


def parse_date(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def get_prices(m):
    raw = m.get("outcomePrices") or "[]"
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        yes = parse_price(prices[0]) if len(prices) > 0 else None
        no  = parse_price(prices[1]) if len(prices) > 1 else None
        if yes is not None and yes < 0:
            yes = None
        if no is not None and no < 0:
            no = None
        if yes == 0.0 and no is not None and no >= 0.99:
            yes = 0.001
        if no == 0.0 and yes is not None and yes >= 0.99:
            no = 0.001
        return yes, no
    except Exception:
        return None, None


def city_is_ready(city, scan_date, today):
    """
    Idéntico a clima-v2:
    - Verifica que la fecha local de la ciudad coincida con scan_date
    - Verifica que la hora Chile esté dentro de la ventana de la ciudad
    - Soporta ventanas que cruzan medianoche (Seoul 23:00-03:00)
    """
    city_offset = CITY_UTC_OFFSET.get(city)
    if city_offset is None:
        return False
    city_local = now_utc() + timedelta(hours=city_offset)
    if city_local.date() != scan_date:
        return False
    win = CITY_WINDOWS.get(city)
    if not win:
        return False
    open_h, open_m, close_h, close_m = win
    chile_now  = now_utc() + timedelta(hours=OBSERVER_UTC_OFFSET)
    c_mins     = chile_now.hour * 60 + chile_now.minute
    open_mins  = open_h  * 60 + open_m
    close_mins = close_h * 60 + close_m
    if open_mins < close_mins:
        return open_mins <= c_mins < close_mins
    else:  # cruza medianoche (Seoul)
        return c_mins >= open_mins or c_mins < close_mins


def build_event_slug(city, date):
    """Nombres de mes completos — idéntico a clima-v2"""
    return f"highest-temperature-in-{city}-on-{MONTHS[date.month]}-{date.day}-{date.year}"


def fetch_event_by_slug(slug):
    try:
        r = requests.get(
            f"{GAMMA}/events",
            params={"slug": slug, "limit": 1},
            timeout=(5, 8),
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
    except Exception:
        pass
    return None


def fetch_market_live(slug):
    try:
        r = requests.get(
            f"{GAMMA}/markets",
            params={"slug": slug, "limit": 1},
            timeout=(5, 8),
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
    except Exception:
        pass
    return None


def fetch_live_prices(slug):
    """Precio vía Gamma (~2 min cache). Fallback cuando CLOB falla."""
    m = fetch_market_live(slug)
    if not m:
        return None, None
    return get_prices(m)


def _fetch_book(yes_token_id):
    """Retorna (bids, asks) del order book CLOB para un token."""
    if not yes_token_id:
        return [], []
    try:
        r = requests.get(
            f"{CLOB}/book",
            params={"token_id": yes_token_id},
            timeout=(2, 3),
        )
        if r.status_code != 200:
            return [], []
        data = r.json()
        return data.get("bids") or [], data.get("asks") or []
    except Exception:
        return [], []


def fetch_yes_price_clob(yes_token_id):
    """
    Precio YES para scanning de nuevas oportunidades.
    Usa best ASK (precio al que compraríamos).
    Descarta si precio > 0.50 (token invertido).
    """
    bids, asks = _fetch_book(yes_token_id)

    yes_price = None
    if asks:
        yes_price = min(float(a["price"]) for a in asks)
    elif bids:
        yes_price = max(float(b["price"]) for b in bids)

    if yes_price is None or not (0.0 < yes_price < 1.0):
        return None, None

    return yes_price, round(1.0 - yes_price, 6)


def fetch_yes_bid_clob(yes_token_id):
    """
    Precio YES de venta para posiciones abiertas.
    Usa best BID (precio real que recibiríamos al vender).
    """
    bids, asks = _fetch_book(yes_token_id)

    yes_price = None
    if bids:
        yes_price = max(float(b["price"]) for b in bids)
    elif asks:
        yes_price = min(float(a["price"]) for a in asks)

    if yes_price is None or not (0.0 < yes_price < 1.0):
        return None, None

    return yes_price, round(1.0 - yes_price, 6)


def scan_opportunities(existing_ids=None, ignore_windows=False):
    """
    Escanea mercados de temperatura YES-side.
    Idéntico a clima-v2:
    - Solo WEATHER_CITIES (8 ciudades) durante sus ventanas horarias (hora Chile)
    - Filtro Gamma: NO 0.88-0.97 (= YES 0.03-0.12) para discovery
    - El entry gate real (YES 0.06-0.115) se aplica en bot.py tras verificar CLOB
    - Retorna candidatos ordenados por YES price ascendente

    ignore_windows=True: omite el check de ventana horaria (solo para test trade)
    """
    if existing_ids is None:
        existing_ids = set()

    today      = now_utc().date()
    scan_dates = [today + timedelta(days=d) for d in range(SCAN_DAYS_AHEAD + 1)]
    opportunities = []

    for scan_date in scan_dates:
        for city in WEATHER_CITIES:
            if not ignore_windows and not city_is_ready(city, scan_date, today):
                continue
            slug  = build_event_slug(city, scan_date)
            event = fetch_event_by_slug(slug)
            if not event:
                continue

            for m in (event.get("markets") or []):
                condition_id = m.get("conditionId")
                if condition_id in existing_ids:
                    continue

                yes_price, no_price = get_prices(m)
                if yes_price is None or no_price is None:
                    continue

                volume = parse_price(m.get("volume") or 0) or 0
                if volume < MIN_VOLUME:
                    continue

                # Filtro Gamma: NO 0.88-0.97 ≈ YES 0.03-0.12
                if not (0.88 <= no_price <= 0.97):
                    continue

                profit_if_tp = (TAKE_PROFIT_YES - yes_price) * 100

                end_dt = parse_date(m.get("endDate"))
                if end_dt and end_dt.date() < today:
                    continue

                raw_ids  = m.get("clobTokenIds") or "[]"
                clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
                yes_token_id = clob_ids[0] if len(clob_ids) > 0 else None
                no_token_id  = clob_ids[1] if len(clob_ids) > 1 else None

                opportunities.append({
                    "condition_id":  condition_id,
                    "city":          city,
                    "question":      m.get("question", ""),
                    "yes_price":     yes_price,
                    "no_price":      no_price,
                    "volume":        volume,
                    "end_date":      end_dt.isoformat() if end_dt else None,
                    "slug":          m.get("slug", ""),
                    "profit_cents":  round(profit_if_tp, 1),
                    "yes_token_id":  yes_token_id,
                    "no_token_id":   no_token_id,
                })

    opportunities.sort(key=lambda x: x["yes_price"])
    return opportunities
