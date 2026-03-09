"""
market_scorer.py — Sistema de scoring 4 factores (0-100 pts)
100% fiel a clima-v2: time score basado en hora local de la ciudad
"""
import threading
import time
from datetime import datetime, timezone, timedelta

from config import (
    SCORE_VOLUME_HIGH, SCORE_VOLUME_MID, SCORE_VOLUME_LOW,
    PRICE_HISTORY_TTL, CITY_UTC_OFFSET,
)

# Historial de precios por condition_id: [(timestamp, yes_price, volume, city)]
_history: dict[str, list] = {}
_lock = threading.Lock()


def record(condition_id: str, yes_price: float, volume: float = 0, city: str = ""):
    """Registra una observación de precio para cálculo de trayectoria."""
    with _lock:
        if condition_id not in _history:
            _history[condition_id] = []
        _history[condition_id].append((time.time(), yes_price, volume, city))
        if len(_history[condition_id]) > 50:
            _history[condition_id].pop(0)


def _price_score(yes_price: float) -> tuple[int, str]:
    """0-30 puntos según zona de precio. Retorna (pts, zone)."""
    if 0.06 <= yes_price <= 0.09:
        return 30, "A"   # Zona A: mayor upside
    if 0.09 < yes_price <= 0.115:
        return 20, "B"   # Zona B: buen upside
    return 0, "-"


def _trajectory_score(condition_id: str) -> int:
    """0-30 puntos según trayectoria reciente del precio."""
    with _lock:
        history = _history.get(condition_id, [])

    if len(history) < 2:
        return 0   # Sin historial suficiente: no asumir tendencia

    recent = [p for _, p, _, _ in history[-10:]]
    if len(recent) < 2:
        return 0

    changes = [recent[i] - recent[i-1] for i in range(1, len(recent))]
    avg_change = sum(changes) / len(changes)

    if 0.005 <= avg_change <= 0.02:
        return 30   # Subida gradual: ideal (0.5-2¢ por observación)
    if -0.005 < avg_change < 0.005:
        return 20   # Estable: bueno
    if avg_change > 0.02:
        return 10   # Subida rápida: puede ser tarde
    return 0        # Bajando: no entrar


def _volume_score(volume: float) -> int:
    """0-20 puntos según volumen."""
    if volume >= SCORE_VOLUME_HIGH:
        return 20
    if volume >= SCORE_VOLUME_MID:
        return 15
    if volume >= SCORE_VOLUME_LOW:
        return 10
    return 0


def _time_score(city: str) -> int:
    """
    0-20 puntos según la hora local de la ciudad.
    Idéntico a clima-v2: premia horas avanzadas del día (temperatura ya definida).
      ≥16h local → 20 pts
      ≥14h local → 15 pts
      ≥12h local → 10 pts
      ≥11h local →  5 pts
      < 11h local →  0 pts
    """
    offset = CITY_UTC_OFFSET.get(city, 0)
    local_hour = (datetime.now(timezone.utc) + timedelta(hours=offset)).hour
    if local_hour >= 16:
        return 20
    if local_hour >= 14:
        return 15
    if local_hour >= 12:
        return 10
    if local_hour >= 11:
        return 5
    return 0


def score(condition_id: str, city: str = "") -> dict:
    """
    Calcula el score total (0-100) y retorna breakdown completo.
    Interfaz compatible con clima-v2: retorna dict con 'total' y 'zone'.
    """
    with _lock:
        history = _history.get(condition_id, [])

    yes_price = history[-1][1] if history else 0.0
    volume    = history[-1][2] if history else 0.0

    ps, zone = _price_score(yes_price)
    ts = _trajectory_score(condition_id)
    vs = _volume_score(volume)
    ti = _time_score(city)

    total = min(ps + ts + vs + ti, 100)

    return {
        "total":      total,
        "zone":       zone,
        "price":      ps,
        "trajectory": ts,
        "volume":     vs,
        "time":       ti,
    }


def score_opportunity(opp: dict) -> dict:
    """
    Registra el precio del opportunity y retorna el score.
    Wrapper conveniente para el bot.
    """
    record(
        opp["condition_id"],
        opp["yes_price"],
        opp.get("volume", 0),
        opp.get("city", ""),
    )
    return score(opp["condition_id"], opp.get("city", ""))


def purge_old(ttl_seconds: int = PRICE_HISTORY_TTL):
    """Elimina historial más antiguo que TTL."""
    now = time.time()
    with _lock:
        for cid in list(_history.keys()):
            _history[cid] = [e for e in _history[cid] if now - e[0] < ttl_seconds]
            if not _history[cid]:
                del _history[cid]


def get_all_scores() -> dict:
    """Retorna historial reciente para dashboard."""
    with _lock:
        return {
            cid: [{"t": e[0], "p": e[1]} for e in pts[-5:]]
            for cid, pts in _history.items()
        }
