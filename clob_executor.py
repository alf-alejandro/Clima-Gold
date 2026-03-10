"""
clob_executor.py — Ejecución real de órdenes en Polymarket CLOB
Usa credenciales de .env (POLYMARKET_KEY + PROXY_ADDRESS)
"""
import os
import math
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
