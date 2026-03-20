"""
clob_executor.py — Ejecución real de órdenes en Polymarket CLOB
Usa credenciales de .env (POLYMARKET_KEY + PROXY_ADDRESS)
"""
import os
import math
import time
import threading
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

HOST     = "https://clob.polymarket.com"
CHAIN_ID = 137
PK       = os.getenv("POLYMARKET_KEY")
PROXY    = os.getenv("PROXY_ADDRESS")

_client      = None
_client_lock = threading.Lock()
_initialized = False


def get_client() -> ClobClient:
    global _client, _initialized
    with _client_lock:
        if not _initialized:
            _client = ClobClient(
                host=HOST, key=PK, chain_id=CHAIN_ID, funder=PROXY, signature_type=1
            )
            _client.set_api_creds(_client.create_or_derive_api_creds())
            _initialized = True
        return _client


def reset_client():
    """Forzar re-inicialización del cliente (útil si falla la conexión)"""
    global _client, _initialized
    with _client_lock:
        _client = None
        _initialized = False


# ─── Allowances ───────────────────────────────────────────────────────────────

def ensure_allowances() -> dict:
    """
    Aprueba el contrato CLOB para mover USDC y YES tokens (conditional).
    Debe llamarse al inicio del bot. Sin esto, los SELL de YES tokens fallan
    con 'not enough balance / allowance'.
    """
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = get_client()
        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── Balance ──────────────────────────────────────────────────────────────────

def get_wallet_info() -> dict:
    """Obtiene el balance real de USDC en la cuenta de Polymarket"""
    try:
        client = get_client()
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        # AssetType.COLLATERAL = USDC en Polymarket
        result  = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = round(int(result.get("balance", 0)) / 1e6, 2)
        return {"balance": balance, "allowance": "aprobado", "status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── Órdenes ──────────────────────────────────────────────────────────────────

def get_best_ask(yes_token_id: str) -> float | None:
    """Obtiene el mejor precio de compra (best ask) del order book público"""
    try:
        r = requests.get(
            f"{HOST}/book",
            params={"token_id": yes_token_id},
            timeout=6
        )
        if r.status_code != 200:
            return None
        data = r.json()
        asks = sorted(data.get("asks", []), key=lambda x: float(x["price"]))
        return float(asks[0]["price"]) if asks else None
    except Exception:
        return None


def get_best_bid(yes_token_id: str) -> float | None:
    """Obtiene el mejor precio de venta (best bid) del order book público"""
    try:
        r = requests.get(
            f"{HOST}/book",
            params={"token_id": yes_token_id},
            timeout=6
        )
        if r.status_code != 200:
            return None
        data = r.json()
        bids = sorted(data.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        return float(bids[0]["price"]) if bids else None
    except Exception:
        return None


def place_market_sell_all(token_id: str, size_tokens: float,
                          max_attempts: int = 20, pause: float = 0.5) -> dict:
    """
    Vende TODA la posición al precio de mercado en loop hasta completar.
    - Cada intento: FOK verificando fill real (no solo orderID).
    - Desescala 0.001 por intento si el FOK no llena.
    - Tras max_attempts: GTC como red de seguridad.
    """
    size_tokens = round(size_tokens, 2)
    client = get_client()
    last_price = 0.01

    for attempt in range(1, max_attempts + 1):
        try:
            bid = get_best_bid(token_id)
            offset = round(0.01 + (attempt - 1) * 0.001, 4)  # 0.010, 0.011, 0.012...
            price  = max(round((bid - offset) if bid else 0.05, 4), 0.01)
            last_price = price

            order_args   = OrderArgs(price=price, size=size_tokens, side=SELL, token_id=token_id)
            signed_order = client.create_order(order_args)
            resp         = client.post_order(signed_order, OrderType.FOK)

            if "orderID" in resp:
                # Verificar que el FOK realmente se llenó (no solo que fue aceptado)
                time.sleep(0.3)
                order_status = get_order_status(resp["orderID"])
                if order_status.get("status") in ("MATCHED", "FILLED"):
                    return {
                        "status":      "ok",
                        "order_id":    resp["orderID"],
                        "size_tokens": size_tokens,
                        "price":       price,
                        "attempts":    attempt,
                    }
                # FOK cancelado — continuar desescalando
        except Exception:
            pass

        time.sleep(pause)

    # Red de seguridad: GTC al último bid conocido
    try:
        bid = get_best_bid(token_id)
        price = max(round((bid - 0.01) if bid else last_price, 4), 0.01)
        order_args   = OrderArgs(price=price, size=size_tokens, side=SELL, token_id=token_id)
        signed_order = client.create_order(order_args)
        resp         = client.post_order(signed_order, OrderType.GTC)
        return {
            "status":      "gtc_fallback",
            "order_id":    resp.get("orderID"),
            "size_tokens": size_tokens,
            "price":       price,
            "attempts":    max_attempts,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "attempts": max_attempts}


def place_buy(token_id: str, price: float, amount_usdc: float) -> dict:
    """Coloca una orden GTC de compra (BUY) de YES tokens"""
    try:
        client = get_client()
        price = round(price, 4)
        size_tokens = math.ceil(amount_usdc / price * 100) / 100  # round up so cost >= amount_usdc
        if size_tokens < 5.0:
            size_tokens = 5.0

        order_args  = OrderArgs(price=price, size=size_tokens, side=BUY, token_id=token_id)
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)

        if "orderID" not in resp:
            return {"status": "error", "error": f"API rechazó la orden: {resp}"}

        return {
            "status": "ok",
            "order_id":    resp["orderID"],
            "size_tokens": size_tokens,
            "price":       price,
            "cost_usdc":   round(size_tokens * price, 4),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def place_maker_sell(token_id: str, size_tokens: float) -> dict:
    """GTC SELL al best_ask - 0.01 — maker, primero en el order book pero más barato"""
    try:
        client = get_client()
        ask = get_best_ask(token_id)
        price = max(round((ask - 0.01) if ask else 0.10, 4), 0.01)
        size_tokens = round(size_tokens, 2)
        order_args   = OrderArgs(price=price, size=size_tokens, side=SELL, token_id=token_id)
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        if "orderID" not in resp:
            return {"status": "error", "error": str(resp)}
        return {"status": "ok", "order_id": resp["orderID"], "price": price, "size_tokens": size_tokens}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def place_sell(token_id: str, price: float, size_tokens: float) -> dict:
    """Coloca una orden GTC de venta (SELL) de YES tokens"""
    try:
        client = get_client()
        price = round(price, 4)
        size_tokens = round(size_tokens, 2)

        order_args   = OrderArgs(price=price, size=size_tokens, side=SELL, token_id=token_id)
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)

        if "orderID" not in resp:
            return {"status": "error", "error": f"API rechazó la orden: {resp}"}

        return {
            "status": "ok",
            "order_id":    resp["orderID"],
            "size_tokens": size_tokens,
            "price":       price,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def cancel_order(order_id: str) -> bool:
    try:
        get_client().cancel(order_id)
        return True
    except Exception:
        return False


def cancel_all() -> bool:
    try:
        get_client().cancel_all()
        return True
    except Exception:
        return False


def get_order_status(order_id: str) -> dict:
    try:
        return get_client().get_order(order_id) or {}
    except Exception:
        return {}
