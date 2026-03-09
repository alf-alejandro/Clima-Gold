"""
bot.py — Loop principal del bot Clima-Gold
Escanea mercados de temperatura, puntúa y abre posiciones reales.
"""
import os
import time
import threading
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

import scanner
import market_scorer
import clob_executor

load_dotenv()

SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL", "30"))   # segundos entre escaneos
PRICE_INTERVAL = int(os.getenv("PRICE_INTERVAL", "10"))  # segundos entre chequeos de precio
MIN_SCORE      = int(os.getenv("MIN_SCORE", "60"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BOT] %(message)s")
log = logging.getLogger("bot")


class BotThread:
    def __init__(self, portfolio):
        self.portfolio   = portfolio
        self._stop_event = threading.Event()
        self._scan_thread  = None
        self._price_thread = None
        self.running = False
        self.status_msg = "Detenido"
        self.last_opportunities: list = []
        self.errors: list = []

    # ─── Control ──────────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return False
        self._stop_event.clear()
        self.running = True
        self.status_msg = "Iniciando..."

        self._scan_thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="scan-loop"
        )
        self._price_thread = threading.Thread(
            target=self._price_loop, daemon=True, name="price-loop"
        )
        self._scan_thread.start()
        self._price_thread.start()
        log.info("Bot iniciado.")
        return True

    def stop(self):
        if not self.running:
            return False
        self._stop_event.set()
        self.running = False
        self.status_msg = "Deteniéndose..."
        # Cancelar todas las órdenes activas
        log.info("Deteniendo bot. Cancelando órdenes abiertas...")
        clob_executor.cancel_all()
        self.status_msg = "Detenido"
        log.info("Bot detenido.")
        return True

    def emergency_stop(self):
        """Pánico total: detiene el bot y cierra todas las posiciones"""
        self.stop()
        self.portfolio.force_close_all()
        log.info("PÁNICO: Todas las posiciones cerradas.")

    # ─── Loops ────────────────────────────────────────────────────────────────

    def _scan_loop(self):
        """Hilo principal: escanea mercados y abre posiciones"""
        while not self._stop_event.is_set():
            try:
                self._cycle()
            except Exception as e:
                msg = f"Error en ciclo: {e}"
                log.error(msg)
                self.errors.append(msg)
                if len(self.errors) > 20:
                    self.errors.pop(0)
            self._stop_event.wait(SCAN_INTERVAL)

    def _price_loop(self):
        """Hilo secundario: monitorea fills y precios"""
        while not self._stop_event.is_set():
            try:
                self.portfolio.check_fills()
            except Exception as e:
                log.error(f"Error en monitoreo de precios: {e}")
            self._stop_event.wait(PRICE_INTERVAL)

    def _cycle(self):
        now = datetime.now(timezone.utc).isoformat()
        self.portfolio._last_scan = now
        self.portfolio._scan_count += 1
        self.status_msg = "Escaneando mercados..."
        log.info(f"Ciclo #{self.portfolio._scan_count} — Escaneando...")

        # Obtener condition_ids ya en portfolio
        tracked = set(
            pos.get("condition_id") for pos in self.portfolio._positions.values()
        )

        # Escanear mercados disponibles
        opps = scanner.scan_all_markets(already_tracked=tracked)
        log.info(f"  {len(opps)} mercados encontrados.")

        # Puntuar
        scored = market_scorer.score_all(opps)
        self.last_opportunities = scored[:20]  # guardar para dashboard

        # Limpiar historial antiguo
        market_scorer.purge_old()

        # Filtrar candidatos
        candidates = [o for o in scored if o["score"] >= MIN_SCORE]
        log.info(f"  {len(candidates)} candidatos con score ≥ {MIN_SCORE}.")

        self.status_msg = f"Activo — {len(self.portfolio._positions)} posiciones abiertas"

        for opp in candidates:
            if not self.portfolio.can_open():
                log.info("  Límite de posiciones alcanzado.")
                break

            log.info(
                f"  Abriendo posición: {opp['city']} "
                f"YES={opp['yes_price']:.4f} score={opp['score']}"
            )
            result = self.portfolio.open_position(opp)

            if result is None:
                log.info("  No se pudo abrir (sin capital o límite alcanzado).")
            elif "error" in result:
                log.warning(f"  Error al abrir: {result['error']}")
            else:
                log.info(
                    f"  ✅ Posición abierta: {result['pos_id']} "
                    f"— Orden {result['buy_order_id']}"
                )
