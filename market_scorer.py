"""
market_scorer.py — Sistema de scoring 4 factores (0-100 pts)
Basado en la estrategia de clima-v2
"""
import threading
import time

# Umbral de puntuación mínima para entrar
MIN_SCORE = 60

# Historial de precios por condition_id (máx 50 obs)
_history: dict[str, list] = {}
_lock = threading.Lock()


def record_price(condition_id: str, yes_price: float):
    """Registra una observación de precio para cálculo de trayectoria"""
    with _lock:
        if condition_id not in _history:
            _history[condition_id] = []
        _history[condition_id].append((time.time(), yes_price))
        if len(_history[condition_id]) > 50:
            _history[condition_id].pop(0)


def _price_score(yes_price: float) -> int:
    """0-30 puntos según zona de precio"""
    if 0.06 <= yes_price <= 0.09:
        return 30   # Zona A: mayor upside
    if 0.09 < yes_price <= 0.12:
        return 20   # Zona B: buen upside
    if 0.03 <= yes_price < 0.06:
        return 10   # Zona C: barato pero raro
    return 0


def _trajectory_score(condition_id: str, yes_price: float) -> int:
    """0-30 puntos según trayectoria reciente del precio"""
    with _lock:
        history = _history.get(condition_id, [])

    if len(history) < 3:
        return 15  # Sin suficiente historial: neutro

    recent = [p for _, p in history[-10:]]
    if len(recent) < 2:
        return 15

    # Cambio promedio entre observaciones consecutivas
    changes = [recent[i] - recent[i-1] for i in range(1, len(recent))]
    avg_change = sum(changes) / len(changes)

    if 0.001 <= avg_change <= 0.02:
        return 30   # Subida gradual: ideal
    if -0.005 < avg_change < 0.001:
        return 20   # Estable: bueno
    if avg_change > 0.02:
        return 10   # Subida rápida: ya puede ser tarde
    return 5        # Bajando: cuidado


def _volume_score(volume: float) -> int:
    """0-20 puntos según volumen"""
    if volume >= 500:
        return 20
    if volume >= 300:
        return 15
    if volume >= 200:
        return 10
    return 0


def _time_score(hours_in_window: float) -> int:
    """0-20 puntos según cuántas horas llevamos en la ventana de trading"""
    if hours_in_window >= 4:
        return 20
    if hours_in_window >= 3:
        return 15
    if hours_in_window >= 2:
        return 10
    if hours_in_window >= 1:
        return 5
    return 0


def score(opportunity: dict) -> int:
    """Calcula el score total (0-100) para una oportunidad"""
    cid   = opportunity["condition_id"]
    price = opportunity["yes_price"]
    vol   = opportunity.get("volume", 0)
    hrs   = opportunity.get("hours_in_window", 0)

    # Registrar precio actual
    record_price(cid, price)

    total = (
        _price_score(price)
        + _trajectory_score(cid, price)
        + _volume_score(vol)
        + _time_score(hrs)
    )
    return min(total, 100)


def score_all(opportunities: list) -> list:
    """Agrega score a cada oportunidad y filtra por MIN_SCORE"""
    result = []
    for opp in opportunities:
        s = score(opp)
        opp["score"] = s
        result.append(opp)
    # Ordenar por score descendente
    result.sort(key=lambda x: x["score"], reverse=True)
    return result


def purge_old(ttl_seconds: int = 3600):
    """Elimina historial más antiguo que TTL"""
    now = time.time()
    with _lock:
        for cid in list(_history.keys()):
            _history[cid] = [(t, p) for t, p in _history[cid] if now - t < ttl_seconds]
            if not _history[cid]:
                del _history[cid]


def get_all_scores() -> dict:
    """Retorna el historial de precios registrado (para debug/dashboard)"""
    with _lock:
        return {
            cid: [{"t": t, "p": p} for t, p in pts[-5:]]
            for cid, pts in _history.items()
        }
