"""
app.py — Flask app para Clima-Gold
Dashboard + API para controlar el bot de temperatura de Polymarket
"""
import os
import io
import csv
from flask import Flask, jsonify, make_response, render_template, request
from dotenv import load_dotenv

import db
import clob_executor
import scanner
import market_scorer
from portfolio import Portfolio
from bot import BotThread

load_dotenv()
db.init_db()

app       = Flask(__name__)
portfolio = Portfolio()
bot       = BotThread(portfolio)


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


# ─── Estado del bot ───────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    stats = portfolio.get_stats()
    return jsonify({
        "bot_running":    bot.running,
        "bot_status":     bot.status_msg,
        "errors":         bot.errors[-5:],
        "opportunities":  bot.last_opportunities[:10],
        **stats,
    })


# ─── Control del bot ──────────────────────────────────────────────────────────

@app.route("/api/bot/start", methods=["POST"])
def api_start():
    ok = bot.start()
    return jsonify({"ok": ok, "message": "Bot iniciado." if ok else "Ya estaba corriendo."})


@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    ok = bot.stop()
    return jsonify({"ok": ok, "message": "Bot detenido." if ok else "Ya estaba detenido."})


@app.route("/api/bot/regime")
def api_regime():
    return jsonify({"regime": bot.active_regime})


# ─── Ver capital real ─────────────────────────────────────────────────────────

@app.route("/api/balance")
def api_balance():
    """Consulta el saldo real de USDC en la cuenta de Polymarket"""
    info = clob_executor.get_wallet_info()
    return jsonify(info)


# ─── Prueba pequeña ───────────────────────────────────────────────────────────

@app.route("/api/test_trade", methods=["POST"])
def api_test_trade():
    """
    Coloca una orden de prueba de $1 en un mercado de temperatura real.
    Permite verificar que la conexión y ejecución funcionan antes de iniciar el bot.
    """
    data = request.get_json(silent=True) or {}
    amount = float(data.get("amount", 1.0))
    amount = max(1.0, min(amount, 10.0))  # entre $1 y $10

    # Buscar cualquier mercado disponible (sin restricción de ventana horaria)
    opps = scanner.scan_opportunities(ignore_windows=True)
    if not opps:
        return jsonify({
            "status": "no_market",
            "message": "No se encontraron mercados de temperatura en Polymarket en este momento."
        })

    # Tomar el primer mercado (menor precio = más tokens por dólar)
    opp = opps[0]
    result = portfolio.test_trade(opp, amount_usdc=amount)
    return jsonify(result)


# ─── Scores actuales ──────────────────────────────────────────────────────────

@app.route("/api/scores")
def api_scores():
    return jsonify(market_scorer.get_all_scores())


# ─── Cancelar orden específica ────────────────────────────────────────────────

@app.route("/api/cancel/<order_id>", methods=["POST"])
def api_cancel(order_id):
    ok = clob_executor.cancel_order(order_id)
    return jsonify({"ok": ok})


# ─── Cancelar todas las órdenes ──────────────────────────────────────────────

@app.route("/api/cancel_all", methods=["POST"])
def api_cancel_all():
    ok = clob_executor.cancel_all()
    return jsonify({"ok": ok, "message": "Todas las órdenes canceladas."})


# ─── Exportar trades como CSV ─────────────────────────────────────────────────

@app.route("/api/trades.csv")
def trades_csv():
    closed = db.load_closed_positions(10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Ciudad", "Pregunta", "Entrada_YES", "Salida_YES", "PnL_USD", "Resultado", "Fecha"])
    for p in closed:
        writer.writerow([
            p.get("city", ""),
            p.get("question", ""),
            p.get("entry_yes", ""),
            p.get("exit_yes", ""),
            p.get("pnl", ""),
            p.get("reason", ""),
            p.get("closed_at", ""),
        ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Disposition"] = "attachment; filename=clima_gold_trades.csv"
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    return resp


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
