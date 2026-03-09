"""
portfolio.py — Gestión de posiciones con ejecución real en el CLOB
"""
import os
import uuid
import threading
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

import db
import clob_executor

load_dotenv()

INITIAL_CAPITAL  = float(os.getenv("INITIAL_CAPITAL", "100.0"))
TAKE_PROFIT      = float(os.getenv("TAKE_PROFIT", "0.15"))
BET_SIZE_MIN_PCT = float(os.getenv("BET_SIZE_MIN_PCT", "0.02"))
BET_SIZE_MAX_PCT = float(os.getenv("BET_SIZE_MAX_PCT", "0.03"))
MAX_POSITIONS    = int(os.getenv("MAX_POSITIONS", "20"))
MIN_POSITION_USD = 1.00  # mínimo absoluto en USDC

_lock = threading.Lock()


class Portfolio:
    def __init__(self):
        db.init_db()
        self._positions: dict = db.load_open_positions()
        self._capital = float(db.get_state("capital") or INITIAL_CAPITAL)
        self._wins = int(db.get_state("wins") or 0)
        self._losses = int(db.get_state("losses") or 0)
        self._total_pnl = float(db.get_state("total_pnl") or 0.0)
        self._scan_count = 0
        self._last_scan = None

    # ─── Capital ──────────────────────────────────────────────────────────────

    def get_capital(self) -> float:
        return self._capital

    def sync_capital_from_chain(self) -> dict:
        """
        Consulta el saldo real de USDC en Polymarket y actualiza el capital disponible.
        El capital = USDC libre en la cuenta (no incluye tokens en posiciones abiertas).
        Se llama automáticamente cada hora desde el bot.
        """
        info = clob_executor.get_wallet_info()
        if info.get("status") != "ok" or info.get("balance") is None:
            return {"ok": False, "error": info.get("error", "Sin datos de balance")}

        real_balance = float(info["balance"])
        with _lock:
            prev = self._capital
            self._capital = round(real_balance, 4)
            db.set_state("capital", str(self._capital))

        log_msg = f"Sync capital: ${prev:.4f} → ${self._capital:.4f} (saldo real Polymarket)"
        return {"ok": True, "prev": prev, "balance": self._capital, "msg": log_msg}

    def _calc_position_size(self, yes_price: float) -> float:
        """Tamaño inverso al precio: más barato = más tokens (más upside)"""
        base_pct = BET_SIZE_MAX_PCT if yes_price < 0.08 else BET_SIZE_MIN_PCT
        amount = self._capital * base_pct
        return max(amount, MIN_POSITION_USD)

    def can_open(self) -> bool:
        return len(self._positions) < MAX_POSITIONS and self._capital >= MIN_POSITION_USD

    # ─── Abrir posición (orden real) ──────────────────────────────────────────

    def open_position(self, opportunity: dict) -> dict | None:
        """
        Coloca una orden real de compra en el CLOB y registra la posición.
        Retorna el dict de posición o None si falla.
        """
        with _lock:
            if not self.can_open():
                return None

            amount = min(self._calc_position_size(opportunity["yes_price"]), self._capital)
            price  = opportunity["yes_price"]

            # Intentar obtener precio más actualizado del CLOB
            clob_price = clob_executor.get_best_ask(opportunity["yes_token_id"])
            if clob_price and 0.03 <= clob_price <= 0.50:
                price = round(clob_price + 0.001, 4)  # 1 tick sobre el best ask
            else:
                price = round(price + 0.001, 4)

            result = clob_executor.place_buy(
                token_id=opportunity["yes_token_id"],
                price=price,
                amount_usdc=amount,
            )

            if result["status"] == "error":
                return {"error": result["error"]}

            pos_id = str(uuid.uuid4())[:8]
            pos = {
                "pos_id":        pos_id,
                "city":          opportunity.get("city", "?"),
                "question":      opportunity.get("question", ""),
                "condition_id":  opportunity["condition_id"],
                "yes_token_id":  opportunity["yes_token_id"],
                "entry_price":   price,
                "amount_usdc":   result["cost_usdc"],
                "tokens":        result["size_tokens"],
                "buy_order_id":  result["order_id"],
                "sell_order_id": None,
                "status":        "pending_buy",  # → in_position → pending_sell → closed
                "current_price": price,
                "score":         opportunity.get("score", 0),
                "opened_at":     datetime.now(timezone.utc).isoformat(),
            }

            self._positions[pos_id] = pos
            self._capital = round(self._capital - result["cost_usdc"], 4)
            db.upsert_open(pos_id, pos)
            db.set_state("capital", str(self._capital))

            return pos

    # ─── Monitoreo de órdenes ─────────────────────────────────────────────────

    def check_fills(self):
        """
        Revisa el estado de todas las órdenes abiertas.
        Las llamadas HTTP se hacen FUERA del lock para no bloquear el scan loop.
        """
        # Snapshot sin lock (solo lectura de IDs)
        with _lock:
            snapshot = list(self._positions.items())

        for pos_id, pos in snapshot:
            try:
                self._check_position(pos_id, pos)
            except Exception:
                pass

    def _check_position(self, pos_id: str, pos: dict):
        status = pos.get("status")

        if status == "pending_buy":
            order = clob_executor.get_order_status(pos["buy_order_id"])
            order_status = order.get("status", "")
            if order_status in ("FILLED", "MATCHED"):
                pos["status"] = "in_position"
                # Colocar orden de venta al take profit
                sell = clob_executor.place_sell(
                    token_id=pos["yes_token_id"],
                    price=TAKE_PROFIT,
                    size_tokens=pos["tokens"],
                )
                if sell["status"] == "ok":
                    pos["sell_order_id"] = sell["order_id"]
                    pos["status"] = "pending_sell"
                db.upsert_open(pos_id, pos)

        elif status in ("in_position", "pending_sell"):
            # Revisar precio actual
            clob_p = clob_executor.get_best_ask(pos["yes_token_id"])
            if clob_p:
                pos["current_price"] = clob_p
                db.upsert_open(pos_id, pos)

            # Revisar si la venta se ejecutó
            if pos.get("sell_order_id"):
                order = clob_executor.get_order_status(pos["sell_order_id"])
                if order.get("status") in ("FILLED", "MATCHED"):
                    self._close_position(pos_id, pos, "take_profit")
                    return

            # Detectar resolución (YES ≥ 0.99 → ganamos, NO ≥ 0.99 → perdemos)
            if clob_p:
                if clob_p >= 0.99:
                    self._close_position(pos_id, pos, "won")
                elif clob_p <= 0.01:
                    self._close_position(pos_id, pos, "lost")

    def _close_position(self, pos_id: str, pos: dict, reason: str):
        """Cierra posición, calcula P&L y actualiza estado"""
        exit_price = pos.get("current_price", pos["entry_price"])
        tokens     = pos["tokens"]
        pnl        = round((exit_price - pos["entry_price"]) * tokens, 4)

        pos["exit_price"]  = exit_price
        pos["pnl"]         = pnl
        pos["reason"]      = reason
        pos["closed_at"]   = datetime.now(timezone.utc).isoformat()

        # Recuperar el valor de mercado actual
        recovered = round(exit_price * tokens, 4)
        self._capital = round(self._capital + recovered, 4)
        self._total_pnl = round(self._total_pnl + pnl, 4)

        if pnl >= 0:
            self._wins += 1
        else:
            self._losses += 1

        db.insert_closed(pos_id, pos)
        db.delete_open(pos_id)
        del self._positions[pos_id]

        db.set_state("capital",   str(self._capital))
        db.set_state("wins",      str(self._wins))
        db.set_state("losses",    str(self._losses))
        db.set_state("total_pnl", str(self._total_pnl))
        db.append_capital(self._capital)

    def force_close_all(self):
        """Pánico: cancela todas las órdenes y cierra posiciones al precio actual"""
        clob_executor.cancel_all()
        with _lock:
            for pos_id in list(self._positions.keys()):
                pos = self._positions.get(pos_id)
                if pos:
                    self._close_position(pos_id, pos, "force_close")

    def force_close_city(self, city: str):
        """Cierra todas las posiciones de una ciudad (ventana expiró)"""
        with _lock:
            city_positions = {
                pos_id: pos for pos_id, pos in self._positions.items()
                if pos.get("city") == city
            }

        for pos_id, pos in city_positions.items():
            # Cancelar solo las órdenes de esta posición (no cancel_all global)
            if pos.get("buy_order_id") and pos.get("status") == "pending_buy":
                clob_executor.cancel_order(pos["buy_order_id"])
            if pos.get("sell_order_id"):
                clob_executor.cancel_order(pos["sell_order_id"])

            # Intentar vender agresivo al precio actual
            cur = pos.get("current_price", pos["entry_price"])
            sell_p = round(cur - 0.01, 4)
            if sell_p > 0.01 and pos.get("status") in ("in_position", "pending_sell"):
                clob_executor.place_sell(pos["yes_token_id"], sell_p, pos["tokens"])

            with _lock:
                if pos_id in self._positions:
                    self._close_position(pos_id, pos, "force_close")

    # ─── Test trade ───────────────────────────────────────────────────────────

    def test_trade(self, opportunity: dict, amount_usdc: float = 1.0) -> dict:
        """
        Coloca una orden de prueba ($1) en un mercado real.
        Sirve para verificar que la conexión y ejecución funcionan.
        """
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

    # ─── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        open_positions = list(self._positions.values())
        closed = db.load_closed_positions(50)
        total_trades = self._wins + self._losses
        win_rate = round(self._wins / total_trades * 100, 1) if total_trades > 0 else 0

        # Valor estimado de posiciones abiertas (tokens × precio actual)
        positions_value = sum(
            pos.get("current_price", pos["entry_price"]) * pos["tokens"]
            for pos in open_positions
        )
        total_portfolio = round(self._capital + positions_value, 4)

        roi = round((total_portfolio - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2)

        return {
            "capital":          round(self._capital, 4),       # USDC libre disponible
            "positions_value":  round(positions_value, 4),     # valor estimado de posiciones
            "total_portfolio":  total_portfolio,               # capital + posiciones
            "initial_capital":  INITIAL_CAPITAL,
            "total_pnl":        round(self._total_pnl, 4),
            "roi_pct":          roi,
            "wins":           self._wins,
            "losses":         self._losses,
            "win_rate":       win_rate,
            "open_count":     len(open_positions),
            "open_positions": open_positions,
            "closed_positions": closed,
            "scan_count":     self._scan_count,
            "last_scan":      self._last_scan,
            "capital_history": db.load_capital_history(),
        }
