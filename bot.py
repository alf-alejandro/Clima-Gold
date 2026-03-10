"""
bot.py — Loop principal Clima-Gold
100% fiel a clima-v2: régimen semana/finde, ventanas hora Chile,
exclusión de condition_ids cerrados, región, auto-liquidación.
"""
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import market_scorer
import clob_executor
from scanner import scan_opportunities, fetch_yes_price_clob, fetch_live_prices
from config import (
    MONITOR_INTERVAL, PRICE_UPDATE_INTERVAL,
    MIN_YES_PRICE, MAX_YES_PRICE,
    MAX_POSITIONS, OBSERVER_UTC_OFFSET, CITY_WINDOWS,
    WEEKEND_ENABLED,
    WEEKDAY_YES_MIN, WEEKDAY_YES_MAX, WEEKDAY_MIN_SCORE,
    WEEKEND_YES_MIN, WEEKEND_YES_MAX, WEEKEND_MIN_SCORE,
)
from portfolio import Portfolio, calc_position_size

log = logging.getLogger(__name__)

MAX_CLOB_VERIFY = 15


# ── Utilidades de tiempo (hora Chile) ────────────────────────────────────────

def chile_mins() -> int:
    """Minutos desde medianoche en hora Chile."""
    now = datetime.now(timezone.utc) + timedelta(hours=OBSERVER_UTC_OFFSET)
    return now.hour * 60 + now.minute


def city_past_close(city: str, c_mins: int) -> bool:
    """
    True si la ventana horaria de la ciudad ya cerró (hora Chile).
    Soporta ventanas que cruzan medianoche (Seoul 23:00-03:00).
    Idéntico a clima-v2.
    """
    win = CITY_WINDOWS.get(city)
    if not win:
        return False
    open_h, open_m, close_h, close_m = win
    open_mins  = open_h  * 60 + open_m
    close_mins = close_h * 60 + close_m
    if open_mins < close_mins:
        return c_mins >= close_mins
    else:  # cruza medianoche
        return close_mins <= c_mins < open_mins


def is_weekend() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5


def get_entry_thresholds():
    """
    Retorna (yes_min, yes_max, min_score, regime_label) según día.
    Si es finde y WEEKEND_ENABLED=False → bloquea entradas.
    Idéntico a clima-v2.
    """
    if is_weekend():
        if WEEKEND_ENABLED:
            return WEEKEND_YES_MIN, WEEKEND_YES_MAX, WEEKEND_MIN_SCORE, "FINDE"
        return None, None, None, "FINDE_BLOQUEADO"
    return WEEKDAY_YES_MIN, WEEKDAY_YES_MAX, WEEKDAY_MIN_SCORE, "SEMANA"


# ── Bot principal ─────────────────────────────────────────────────────────────

class BotThread:
    def __init__(self, portfolio: Portfolio):
        self.portfolio          = portfolio
        self._stop_event        = threading.Event()
        self._scan_thread       = None
        self._price_thread      = None
        self.running            = False
        self.status_msg         = "Detenido"
        self.active_regime      = "—"
        self.last_opportunities: list = []
        self.errors:            list = []
        self._last_capital_sync = 0.0

    # ── Control ───────────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return False
        self._stop_event.clear()
        self.running = True
        self.status_msg = "Iniciando..."
        self._last_capital_sync = 0.0  # forzar sync inmediato

        self._price_thread = threading.Thread(
            target=self._run_prices, daemon=True, name="price-loop"
        )
        self._scan_thread = threading.Thread(
            target=self._run, daemon=True, name="scan-loop"
        )
        self._price_thread.start()
        self._scan_thread.start()
        log.info("Bot iniciado.")
        return True

    def stop(self):
        if not self.running:
            return False
        self._stop_event.set()
        self.running = False
        self.status_msg = "Detenido"
        log.info("Bot detenido.")
        return True

    # ── Loops ─────────────────────────────────────────────────────────────────

    def _run(self):
        log.info("Scan loop iniciado — 8 ciudades · ventanas hora Chile · Score-Filtered YES")
        while not self._stop_event.is_set():
            try:
                self._cycle()
            except Exception:
                log.exception("Error en ciclo")
            self._stop_event.wait(MONITOR_INTERVAL)
        log.info("Scan loop detenido.")

    def _run_prices(self):
        log.info("Price updater iniciado.")
        while not self._stop_event.is_set():
            self._stop_event.wait(PRICE_UPDATE_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._refresh_prices()
            except Exception:
                log.exception("Error actualizando precios")
        log.info("Price updater detenido.")

    # ── Ciclo principal ───────────────────────────────────────────────────────

    def _cycle(self):
        portfolio = self.portfolio
        portfolio._scan_count += 1
        portfolio._last_scan   = datetime.now(timezone.utc).isoformat()

        # Watchdog del price thread
        if self._price_thread and not self._price_thread.is_alive():
            log.warning("Price thread caído — reiniciando")
            self._price_thread = threading.Thread(
                target=self._run_prices, daemon=True, name="price-loop"
            )
            self._price_thread.start()

        # Sync capital cada hora
        self._maybe_sync_capital()

        # Régimen activo
        yes_min, yes_max, min_score, regime = get_entry_thresholds()
        self.active_regime = regime
        entries_blocked = (yes_min is None)
        if entries_blocked:
            log.info("Régimen FINDE_BLOQUEADO — sin nuevas entradas")
        self.status_msg = f"Activo [{regime}] — {len(portfolio.positions)} posiciones"

        # IDs a saltar: abiertos + cerrados (no re-entrar mercados ya operados)
        with portfolio.lock:
            existing_ids = set(portfolio.positions.keys())
            closed_ids   = {
                p.get("condition_id") for p in portfolio.closed_positions
                if p.get("condition_id")
            }
            existing_ids |= closed_ids

        # 1. Gamma discovery
        opportunities = scan_opportunities(existing_ids)

        # 2. CLOB + scoring
        with portfolio.lock:
            open_count = len(portfolio.positions)
        slots_available = max(0, MAX_POSITIONS - open_count)
        verify_n   = min(len(opportunities), max(slots_available, MAX_CLOB_VERIFY))
        candidates = opportunities[:verify_n]

        verified_opps = []
        display_opps  = []
        clob_ok       = True
        clob_fails    = 0

        for opp in candidates:
            if self._stop_event.is_set():
                return

            yes_tid = opp.get("yes_token_id")
            rt_yes, rt_no = None, None

            if clob_ok and yes_tid:
                rt_yes, rt_no = fetch_yes_price_clob(yes_tid)
                if rt_yes is not None and rt_yes > 0.50:
                    rt_yes, rt_no = None, None
                if rt_yes is None:
                    clob_fails += 1
                    if clob_fails >= 5:
                        clob_ok = False

            if rt_yes is None:
                display_opps.append({**opp, "score": 0, "zone": "-"})
                continue

            # Registrar en scorer (siempre, para acumular historial de trayectoria)
            market_scorer.record(opp["condition_id"], rt_yes, opp["volume"], opp.get("city", ""))

            opp = {**opp, "yes_price": rt_yes, "no_price": rt_no or round(1 - rt_yes, 4)}

            sc          = market_scorer.score(opp["condition_id"], opp.get("city", ""))
            score_total = sc["total"]

            display_opps.append({**opp, "score": score_total, "zone": sc["zone"]})

            if entries_blocked:
                continue

            if not (yes_min <= rt_yes <= yes_max):
                continue

            if score_total < min_score:
                continue

            opp["score"] = score_total
            opp["zone"]  = sc["zone"]
            log.info(
                "Candidato [%s] %s — YES=%.1f¢ score=%d zona=%s",
                regime, opp["question"][:40], rt_yes * 100, score_total, sc["zone"],
            )
            verified_opps.append(opp)

        display_opps.extend(opportunities[verify_n:verify_n + (20 - len(display_opps))])
        self.last_opportunities = [
            {
                "question":     o["question"],
                "city":         o.get("city", ""),
                "yes_price":    o["yes_price"],
                "no_price":     o["no_price"],
                "volume":       o["volume"],
                "profit_cents": o.get("profit_cents", 0),
                "score":        o.get("score", 0),
                "zone":         o.get("zone", "-"),
            }
            for o in display_opps[:20]
        ]

        # 3. Precios de posiciones abiertas (para cierre forzoso y exits)
        with portfolio.lock:
            pos_data = [
                (pos.get("condition_id") or pid, pos.get("yes_token_id"), pos.get("slug"))
                for pid, pos in portfolio.positions.items()
            ]

        price_map     = {}
        clob_ok_pos   = True
        clob_fail_pos = 0
        for cid, yes_tid, slug in pos_data:
            if self._stop_event.is_set():
                return
            yes_p, no_p = None, None
            if clob_ok_pos and yes_tid:
                yes_p, no_p = fetch_yes_price_clob(yes_tid)
                if yes_p is not None and yes_p > 0.50:
                    yes_p, no_p = None, None
                if yes_p is None:
                    clob_fail_pos += 1
                    if clob_fail_pos >= 2:
                        clob_ok_pos = False
            if yes_p is None:
                yes_p, no_p = fetch_live_prices(slug)
            if yes_p is not None and no_p is not None:
                price_map[cid] = (yes_p, no_p)

        # 4a. Actualizar precios → recoger exits TP (con lock, sin HTTP)
        tp_exits = []
        with portfolio.lock:
            if price_map:
                tp_exits = portfolio.apply_price_updates(price_map)

        # 4b. Colocar sells al precio de mercado para TP exits (HTTP, sin lock)
        sells_done = []
        for pos_id, yes_p, tokens, token_id, allocated in tp_exits:
            if pos_id not in portfolio.positions:
                continue  # ya cerrado (p.ej. por price thread)
            if token_id:
                sell_price = max(round(yes_p - 0.002, 4), 0.02)
                clob_executor.place_sell(token_id, sell_price, tokens)
                log.info(
                    "TP sell @ %.1f¢ (entrada ~%.1f¢) — %s",
                    yes_p * 100, allocated / tokens * 100 if tokens else 0,
                    pos_id,
                )
            sells_done.append((pos_id, yes_p, tokens, allocated))

        # 4c. Portfolio operations (con lock)
        with portfolio.lock:

            # Cerrar posiciones TP al precio capturado
            for pos_id, yes_p, tokens, allocated in sells_done:
                if pos_id in portfolio.positions:
                    pnl = round(tokens * yes_p - allocated, 4)
                    portfolio._close_position(
                        pos_id, "TAKE_PROFIT", pnl,
                        resolution=f"Take profit @ YES={yes_p*100:.1f}¢",
                    )

            # Abrir nuevas posiciones
            for opp in verified_opps:
                if not portfolio.can_open_position():
                    break
                if not portfolio.region_has_capacity(opp.get("city", "")):
                    log.debug("Región llena, skip %s", opp.get("city"))
                    continue
                amount = calc_position_size(portfolio.capital_disponible, opp["yes_price"])
                if amount >= 0.50:
                    pos = portfolio.open_position(opp, amount)
                    if pos:
                        log.info(
                            "Abierta YES: %s [%s] @ %.1f¢ $%.2f score=%d zona=%s",
                            opp["question"][:40], regime,
                            opp["yes_price"] * 100, amount,
                            opp["score"], opp["zone"],
                        )

            # Auto-liquidar posiciones con entrada fuera de rango (robustez)
            for pid, pos in list(portfolio.positions.items()):
                entry_yes = pos.get("entry_yes", 0.0)
                if not (MIN_YES_PRICE <= entry_yes <= MAX_YES_PRICE):
                    current_yes = pos.get("current_yes", entry_yes)
                    pnl = round(pos["tokens"] * current_yes - pos["allocated"], 4)
                    log.warning(
                        "Auto-liquidar %s — entrada YES=%.1f¢ fuera de rango",
                        pos["question"][:40], entry_yes * 100,
                    )
                    portfolio._close_position(
                        pid, "LIQUIDATED", pnl,
                        resolution=f"Entrada YES {entry_yes*100:.1f}¢ fuera de rango"
                    )

            # Cierres forzosos por ciudad (ventana expirada, hora Chile)
            mins = chile_mins()
            for city in list({pos.get("city") for pos in portfolio.positions.values()}):
                if city and city_past_close(city, mins):
                    positions_in_city = [
                        pid for pid, pos in portfolio.positions.items()
                        if pos.get("city") == city
                    ]
                    if positions_in_city:
                        log.info(
                            "Cierre forzoso ventana [%s] — %d posición(es)",
                            city, len(positions_in_city),
                        )
                        portfolio.force_close_city(city, mins)

            portfolio.record_capital()

        market_scorer.purge_old()

    # ── Price refresh loop ────────────────────────────────────────────────────

    def _refresh_prices(self):
        """Hilo secundario: refresca precios de posiciones abiertas."""
        with self.portfolio.lock:
            pos_data = [
                (pos.get("condition_id") or pid, pos.get("yes_token_id"), pos.get("slug"))
                for pid, pos in self.portfolio.positions.items()
            ]

        clob_ok       = True
        clob_failures = 0
        price_map     = {}

        for cid, yes_tid, slug in pos_data:
            if self._stop_event.is_set():
                return
            yes_p, no_p = None, None
            if clob_ok and yes_tid:
                yes_p, no_p = fetch_yes_price_clob(yes_tid)
                if yes_p is not None and yes_p > 0.50:
                    yes_p, no_p = None, None
                    clob_failures += 1
                elif yes_p is not None:
                    clob_failures = 0
                else:
                    clob_failures += 1
                if clob_failures >= 2:
                    clob_ok = False
            if yes_p is None:
                yes_p, no_p = fetch_live_prices(slug)
            if yes_p is not None and no_p is not None:
                price_map[cid] = (yes_p, no_p)

        tp_exits = []
        if price_map:
            with self.portfolio.lock:
                tp_exits = self.portfolio.apply_price_updates(price_map)

        # Sells al precio de mercado para TP (HTTP, sin lock)
        sells_done = []
        for pos_id, yes_p, tokens, token_id, allocated in tp_exits:
            if pos_id not in self.portfolio.positions:
                continue
            if token_id:
                sell_price = max(round(yes_p - 0.002, 4), 0.02)
                clob_executor.place_sell(token_id, sell_price, tokens)
            sells_done.append((pos_id, yes_p, tokens, allocated))

        if sells_done:
            with self.portfolio.lock:
                for pos_id, yes_p, tokens, allocated in sells_done:
                    if pos_id in self.portfolio.positions:
                        pnl = round(tokens * yes_p - allocated, 4)
                        self.portfolio._close_position(
                            pos_id, "TAKE_PROFIT", pnl,
                            resolution=f"Take profit @ YES={yes_p*100:.1f}¢",
                        )

        # check fills (fuera del lock principal)
        self.portfolio.check_fills()

    # ── Capital sync horario ──────────────────────────────────────────────────

    def _maybe_sync_capital(self):
        now = time.time()
        if now - self._last_capital_sync < 3600:
            return
        log.info("Sincronizando capital con saldo real de Polymarket...")
        result = self.portfolio.sync_capital_from_chain()
        if result["ok"]:
            log.info("  %s", result["msg"])
        else:
            log.warning("  Sync fallido: %s. Usando capital interno.", result.get("error"))
        self._last_capital_sync = now
