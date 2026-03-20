"""
portfolio.py — Gestión de posiciones con ejecución real en el CLOB
Sizing y región idénticos a clima-v2. Capital se sincroniza desde Polymarket cada hora.
"""
import uuid
import threading
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

import db
import clob_executor
from config import (
    INITIAL_CAPITAL, TAKE_PROFIT_YES,
    POSITION_SIZE_MIN, POSITION_SIZE_MAX,
    MIN_YES_PRICE, MAX_YES_PRICE,
    MAX_POSITIONS, MAX_REGION_EXPOSURE, REGION_MAP,
    BUY_TIMEOUT_MINUTES, MAKER_SELL_STOP_LOSS,
)

load_dotenv()
log  = logging.getLogger(__name__)
lock = threading.Lock()


def calc_position_size(capital_disponible: float, yes_price: float) -> float:
    """
    Interpolación lineal idéntica a clima-v2:
      YES=0.06  → 3.0% del capital  (más barato → más tokens → más upside)
      YES=0.115 → 2.0% del capital
    """
    price_range = MAX_YES_PRICE - MIN_YES_PRICE
    if price_range <= 0:
        pct = POSITION_SIZE_MAX
    else:
        t   = (MAX_YES_PRICE - yes_price) / price_range
        t   = max(0.0, min(1.0, t))
        pct = POSITION_SIZE_MIN + t * (POSITION_SIZE_MAX - POSITION_SIZE_MIN)
    return capital_disponible * pct


class Portfolio:
    def __init__(self):
        db.init_db()
        self._positions: dict    = db.load_open_positions()
        self._closed:    list    = db.load_closed_positions(500)
        self._capital            = float(db.get_state("capital") or INITIAL_CAPITAL)
        self._initial_capital    = INITIAL_CAPITAL
        self._wins               = int(db.get_state("wins")      or 0)
        self._losses             = int(db.get_state("losses")    or 0)
        self._total_pnl          = float(db.get_state("total_pnl") or 0.0)
        self._scan_count         = 0
        self._last_scan          = None

    # ── Acceso al lock (bot.py lo usa directamente como en v2) ───────────────

    @property
    def lock(self):
        return lock

    @property
    def positions(self):
        return self._positions

    @property
    def closed_positions(self):
        return self._closed

    @property
    def capital_disponible(self):
        return self._capital

    # ── Constraints ───────────────────────────────────────────────────────────

    def can_open_position(self) -> bool:
        return len(self._positions) < MAX_POSITIONS and self._capital >= 0.50

    def already_in_market(self, condition_id: str) -> bool:
        """Evita duplicados en el mismo mercado."""
        return any(
            pos.get("condition_id") == condition_id
            for pos in self._positions.values()
        )

    def region_has_capacity(self, city: str) -> bool:
        """Máximo MAX_REGION_EXPOSURE (25%) del capital total por región."""
        region = REGION_MAP.get(city)
        if not region:
            return True
        allocated = sum(
            pos.get("allocated", 0)
            for pos in self._positions.values()
            if REGION_MAP.get(pos.get("city", "")) == region
        )
        capital_total = self._capital + sum(
            pos.get("allocated", 0) for pos in self._positions.values()
        )
        return allocated < capital_total * MAX_REGION_EXPOSURE

    # ── Abrir posición (orden real en CLOB) ───────────────────────────────────

    def open_position(self, opportunity: dict, amount: float) -> dict | None:
        """
        Coloca orden real de compra en el CLOB y registra la posición.
        El amount viene calculado externamente (idéntico a cómo lo hace v2).
        """
        price = opportunity["yes_price"]

        # Precio más actualizado del CLOB (1 tick sobre best ask)
        clob_price = clob_executor.get_best_ask(opportunity["yes_token_id"])
        if clob_price and 0.03 <= clob_price <= 0.50:
            price = round(clob_price + 0.001, 4)
        else:
            price = round(price + 0.001, 4)

        result = clob_executor.place_buy(
            token_id=opportunity["yes_token_id"],
            price=price,
            amount_usdc=amount,
        )

        if result["status"] == "error":
            log.warning("Error al abrir posición: %s", result["error"])
            return None

        pos_id = str(uuid.uuid4())[:8]
        pos = {
            "pos_id":        pos_id,
            "city":          opportunity.get("city", ""),
            "question":      opportunity.get("question", ""),
            "condition_id":  opportunity["condition_id"],
            "yes_token_id":  opportunity["yes_token_id"],
            "slug":          opportunity.get("slug", ""),
            "entry_yes":     price,
            "current_yes":   price,
            "allocated":     result["cost_usdc"],
            "tokens":        result["size_tokens"],
            "buy_order_id":  result["order_id"],
            "sell_order_id": None,
            "status":        "pending_buy",
            "score":         opportunity.get("score", 0),
            "zone":          opportunity.get("zone", "-"),
            "opened_at":     datetime.now(timezone.utc).isoformat(),
        }

        self._positions[pos_id] = pos
        self._capital = round(self._capital - result["cost_usdc"], 4)
        db.upsert_open(pos_id, pos)
        db.set_state("capital", str(self._capital))
        return pos

    # ── Monitoreo de órdenes y precios ────────────────────────────────────────

    def apply_price_updates(self, price_map: dict) -> list:
        """
        Aplica actualizaciones de precio.
        - WON / LOST: cierra internamente (resolución de mercado).
        - TAKE_PROFIT: retorna lista para que el caller coloque sell de mercado
          al precio actual antes de cerrar internamente.
        Retorna: [(pos_id, yes_p, tokens, yes_token_id, allocated), ...]
        """
        tp_exits   = []
        loss_exits = []
        for pos_id, pos in list(self._positions.items()):
            cid = pos.get("condition_id") or pos_id
            if cid not in price_map:
                continue

            yes_p, no_p = price_map[cid]
            pos["current_yes"] = yes_p
            db.upsert_open(pos_id, pos)

            if yes_p >= 0.99:
                pnl = round(pos["tokens"] * yes_p - pos["allocated"], 4)
                self._close_position(pos_id, "WON", pnl,
                    resolution=f"YES resuelto ≥0.99 @ {yes_p:.4f}")

            elif no_p is not None and no_p >= 0.99:
                # Intentar vender antes de cerrar para recuperar lo que quede
                loss_exits.append((
                    pos_id, yes_p, pos["tokens"],
                    pos.get("yes_token_id"), pos["allocated"],
                ))

            elif yes_p >= TAKE_PROFIT_YES:
                if pos.get("status") != "pending_sell":  # no duplicar orden maker
                    tp_exits.append((
                        pos_id, yes_p, pos["tokens"],
                        pos.get("yes_token_id"), pos["allocated"],
                    ))

        return tp_exits, loss_exits

    def set_pending_sell(self, pos_id: str, sell_order_id: str, maker_price: float):
        """Marca posición como pending_sell con la orden maker GTC colocada."""
        pos = self._positions.get(pos_id)
        if not pos:
            return
        pos["sell_order_id"]     = sell_order_id
        pos["maker_entry_price"] = maker_price   # precio cuando se activó el TP
        pos["status"]            = "pending_sell"
        db.upsert_open(pos_id, pos)

    def check_fills(self):
        """
        Monitorea fills de órdenes BUY y SELL pendientes.
        HTTP fuera del lock para no bloquear el scan loop.
        """
        with lock:
            snapshot = list(self._positions.items())

        now = datetime.now(timezone.utc)
        for pos_id, pos in snapshot:
            status = pos.get("status")

            # ── Monitorear orden de venta maker (pending_sell) ────────────────
            if status == "pending_sell" and pos.get("sell_order_id"):
                try:
                    order = clob_executor.get_order_status(pos["sell_order_id"])
                    order_status = order.get("status", "")

                    if order_status in ("FILLED", "MATCHED"):
                        # Orden llenada — cerrar posición con precio real del fill
                        fill_price = float(order.get("price") or pos.get("current_yes", pos["entry_yes"]))
                        with lock:
                            if pos_id in self._positions:
                                p = self._positions[pos_id]
                                pnl = round(p["tokens"] * fill_price - p["allocated"], 4)
                                self._close_position(
                                    pos_id, "TAKE_PROFIT", pnl,
                                    resolution=f"Maker sell llenado @ {fill_price*100:.1f}¢",
                                )
                        log.info("Maker sell llenado @ %.1f¢ — %s", fill_price * 100, pos_id)
                    else:
                        # Aún sin llenar — cancelar y evaluar stop-loss
                        clob_executor.cancel_order(pos["sell_order_id"])

                        maker_entry   = pos.get("maker_entry_price") or pos.get("current_yes", pos["entry_yes"])
                        current_price = pos.get("current_yes", maker_entry)
                        stop_threshold = round(maker_entry * (1 - MAKER_SELL_STOP_LOSS), 4)

                        if current_price < stop_threshold:
                            # Stop-loss: precio cayó >30% desde el TP → FOK inmediato
                            result = clob_executor.place_market_sell_all(
                                pos["yes_token_id"], pos["tokens"]
                            )
                            fill_price = result.get("price")
                            if not fill_price:
                                # FOK sin fill — re-colocar maker al precio actual
                                log.warning(
                                    "Stop-loss FOK sin fill (%.1f¢ umbral) — reintentando maker @ %.1f¢ — %s",
                                    stop_threshold * 100, current_price * 100, pos_id,
                                )
                                new_result = clob_executor.place_maker_sell(
                                    pos["yes_token_id"], pos["tokens"]
                                )
                                if new_result["status"] == "ok":
                                    with lock:
                                        if pos_id in self._positions:
                                            self._positions[pos_id]["sell_order_id"] = new_result["order_id"]
                                            db.upsert_open(pos_id, self._positions[pos_id])
                            else:
                                with lock:
                                    if pos_id in self._positions:
                                        p   = self._positions[pos_id]
                                        pnl = round(p["tokens"] * fill_price - p["allocated"], 4)
                                        self._close_position(
                                            pos_id, "TAKE_PROFIT", pnl,
                                            resolution=f"Maker stop-loss FOK @ {fill_price*100:.1f}¢ "
                                                       f"(TP entrada={maker_entry*100:.1f}¢)",
                                        )
                                log.warning(
                                    "Maker stop-loss FOK @ %.1f¢ (TP entrada=%.1f¢, umbral=%.1f¢) — %s",
                                    fill_price * 100, maker_entry * 100, stop_threshold * 100, pos_id,
                                )
                        else:
                            # Sin stop-loss — re-colocar maker al nuevo best_ask - 0.01
                            new_result = clob_executor.place_maker_sell(
                                pos["yes_token_id"], pos["tokens"]
                            )
                            if new_result["status"] == "ok":
                                with lock:
                                    if pos_id in self._positions:
                                        self._positions[pos_id]["sell_order_id"] = new_result["order_id"]
                                        db.upsert_open(pos_id, self._positions[pos_id])
                                log.info(
                                    "Maker sell actualizado @ %.1f¢ — %s",
                                    new_result["price"] * 100, pos_id,
                                )
                except Exception:
                    pass
                continue

            if status != "pending_buy":
                continue

            # ── Monitorear orden de compra (pending_buy) ──────────────────────
            try:
                order = clob_executor.get_order_status(pos["buy_order_id"])
                if order.get("status") in ("FILLED", "MATCHED"):
                    with lock:
                        if pos_id in self._positions:
                            self._positions[pos_id]["status"] = "in_position"
                            db.upsert_open(pos_id, self._positions[pos_id])
                    continue
            except Exception:
                pass

            # Cancelar compra si lleva más de BUY_TIMEOUT_MINUTES sin llenarse
            try:
                opened_at = datetime.fromisoformat(pos["opened_at"])
                if (now - opened_at).total_seconds() > BUY_TIMEOUT_MINUTES * 60:
                    clob_executor.cancel_order(pos["buy_order_id"])
                    with lock:
                        if pos_id in self._positions:
                            self._capital = round(self._capital + pos["allocated"], 4)
                            db.set_state("capital", str(self._capital))
                            db.delete_open(pos_id)
                            del self._positions[pos_id]
                    log.info(
                        "Compra cancelada por timeout (%dm): %s [%s] @ %.1f¢",
                        BUY_TIMEOUT_MINUTES, pos_id,
                        pos.get("city", ""), pos.get("entry_yes", 0) * 100,
                    )
            except Exception:
                pass

    # ── Cierre de posición ────────────────────────────────────────────────────

    def _close_position(self, pos_id: str, reason: str, pnl: float,
                        resolution: str = ""):
        """Cierra posición, actualiza capital y persiste. Debe llamarse con lock."""
        pos = self._positions.get(pos_id)
        if not pos:
            return

        exit_yes  = pos.get("current_yes", pos["entry_yes"])
        recovered = round(exit_yes * pos["tokens"], 4)

        pos["exit_yes"]   = exit_yes
        pos["pnl"]        = round(pnl, 4)
        pos["reason"]     = reason
        pos["resolution"] = resolution
        pos["closed_at"]  = datetime.now(timezone.utc).isoformat()

        self._capital    = round(self._capital + recovered, 4)
        self._total_pnl  = round(self._total_pnl + pnl, 4)

        if pnl >= 0:
            self._wins += 1
        else:
            self._losses += 1

        self._closed.insert(0, pos)
        if len(self._closed) > 500:
            self._closed.pop()

        db.insert_closed(pos_id, pos)
        db.delete_open(pos_id)
        del self._positions[pos_id]

        db.set_state("capital",   str(self._capital))
        db.set_state("wins",      str(self._wins))
        db.set_state("losses",    str(self._losses))
        db.set_state("total_pnl", str(self._total_pnl))
        db.append_capital(self._capital)

    # ── Cierre forzoso por ciudad (ventana expiró) ────────────────────────────

    def force_close_city(self, city: str, mins_chile: int):
        """Cierra todas las posiciones de una ciudad. Debe llamarse con lock."""
        for pos_id, pos in list(self._positions.items()):
            if pos.get("city") != city:
                continue

            # Cancelar órdenes individuales (no cancel_all para no afectar otras ciudades)
            if pos.get("buy_order_id") and pos.get("status") == "pending_buy":
                clob_executor.cancel_order(pos["buy_order_id"])
            if pos.get("sell_order_id"):
                clob_executor.cancel_order(pos["sell_order_id"])

            current_yes = pos.get("current_yes", pos["entry_yes"])

            # Venta FOK agresiva si tenemos tokens
            if pos.get("status") in ("in_position", "pending_sell") and pos.get("yes_token_id"):
                result = clob_executor.place_market_sell_all(pos["yes_token_id"], pos["tokens"])
                if result.get("price"):
                    current_yes = result["price"]
                elif result.get("status") == "error" or not result.get("price"):
                    # FOK no se llenó — dejar posición activa, solo limpiar orden maker
                    pos["sell_order_id"] = None
                    pos["status"]        = "in_position"
                    db.upsert_open(pos_id, pos)
                    log.warning(
                        "Force_close FOK sin fill [%s] %s — posición mantenida",
                        city, pos_id,
                    )
                    continue

            pnl = round(pos["tokens"] * current_yes - pos["allocated"], 4)
            sign = "+" if pnl >= 0 else ""
            self._close_position(
                pos_id, "FORCE_CLOSE", pnl,
                resolution=f"Cierre forzoso ventana {city} · YES={current_yes*100:.1f}¢ ({sign}{pnl:.2f})"
            )

    # ── Capital sync desde Polymarket (exclusivo Clima-Gold) ─────────────────

    def sync_capital_from_chain(self) -> dict:
        """
        Consulta saldo real de USDC en Polymarket y actualiza el capital disponible.
        Se llama cada hora automáticamente.
        """
        info = clob_executor.get_wallet_info()
        if info.get("status") != "ok" or info.get("balance") is None:
            return {"ok": False, "error": info.get("error", "Sin datos")}

        real_balance = float(info["balance"])
        with lock:
            prev = self._capital
            self._capital = round(real_balance, 4)
            db.set_state("capital", str(self._capital))

        msg = f"Sync capital: ${prev:.4f} → ${self._capital:.4f} (saldo real Polymarket)"
        return {"ok": True, "prev": prev, "balance": self._capital, "msg": msg}

    # ── Test trade (verificación antes de iniciar bot) ────────────────────────

    def test_trade(self, opportunity: dict, amount_usdc: float = 1.0) -> dict:
        price  = opportunity["yes_price"]
        clob_p = clob_executor.get_best_ask(opportunity["yes_token_id"])
        if clob_p:
            price = round(clob_p, 4)
        result = clob_executor.place_buy(
            token_id=opportunity["yes_token_id"],
            price=price,
            amount_usdc=amount_usdc,
        )
        result["market"] = opportunity.get("question", opportunity.get("city", "?"))
        result["city"]   = opportunity.get("city", "?")
        return result

    # ── Stats para dashboard ──────────────────────────────────────────────────

    def record_capital(self):
        db.append_capital(self._capital)

    def get_stats(self) -> dict:
        open_positions = list(self._positions.values())
        closed         = self._closed
        total_trades   = self._wins + self._losses
        win_rate       = round(self._wins / total_trades * 100, 1) if total_trades else 0

        positions_value = sum(
            pos.get("current_yes", pos["entry_yes"]) * pos["tokens"]
            for pos in open_positions
        )
        total_portfolio = round(self._capital + positions_value, 4)
        roi = round((total_portfolio - self._initial_capital) / self._initial_capital * 100, 2)

        # Profit Factor
        gross_wins   = sum(p.get("pnl", 0) for p in closed if (p.get("pnl") or 0) > 0)
        gross_losses = abs(sum(p.get("pnl", 0) for p in closed if (p.get("pnl") or 0) < 0))
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else None

        # Max Drawdown
        history    = db.load_capital_history()
        peak       = self._initial_capital
        max_dd_pct = 0.0
        for h in history:
            if h["capital"] > peak:
                peak = h["capital"]
            if peak > 0:
                dd = (peak - h["capital"]) / peak * 100
                if dd > max_dd_pct:
                    max_dd_pct = dd

        # P&L por ciudad
        city_stats: dict = {}
        for p in closed:
            city = p.get("city", "?")
            if city not in city_stats:
                city_stats[city] = {"wins": 0, "losses": 0, "pnl": 0.0}
            pnl = p.get("pnl") or 0
            city_stats[city]["pnl"] = round(city_stats[city]["pnl"] + pnl, 4)
            if pnl > 0:
                city_stats[city]["wins"]   += 1
            else:
                city_stats[city]["losses"] += 1

        return {
            "capital":          round(self._capital, 4),
            "positions_value":  round(positions_value, 4),
            "total_portfolio":  total_portfolio,
            "initial_capital":  self._initial_capital,
            "total_pnl":        round(self._total_pnl, 4),
            "roi_pct":          roi,
            "wins":             self._wins,
            "losses":           self._losses,
            "win_rate":         win_rate,
            "profit_factor":    profit_factor,
            "max_drawdown_pct": round(max_dd_pct, 2),
            "gross_wins":       round(gross_wins, 4),
            "gross_losses":     round(gross_losses, 4),
            "city_stats":       city_stats,
            "open_count":       len(open_positions),
            "open_positions":   open_positions,
            "closed_positions": closed[:50],
            "scan_count":       self._scan_count,
            "last_scan":        self._last_scan,
            "capital_history":  history,
        }
