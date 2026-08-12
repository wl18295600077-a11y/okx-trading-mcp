#!/usr/bin/env python3
"""
okx_utils.py — shared helpers for OKX Trading MCP Pro tools
============================================================
Universal credential loading + exchange factory + indicator math.

Credentials are resolved from (first match wins):
  1. environment variables          (OKX_API_KEY / OKX_SECRET / OKX_PASSPHRASE)
  2. .env next to this file         (created by setup_env.py)
  3. ~/.okx-trader/.env             (user-global)

Never uploads credentials anywhere — everything stays on your machine.
"""
import os

ENV_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    os.path.expanduser("~/.okx-trader/.env"),
    os.path.expanduser("~/.hermes/.env"),  # legacy (Hermes)
]


def load_env():
    """Load OKX credentials from the first .env that exists (no overwrite)."""
    for path in ENV_CANDIDATES:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip('"').strip("'")
                            if k and not os.environ.get(k):
                                os.environ[k] = v
                break
            except OSError:
                continue


def get_exchange():
    """Create and return an authenticated OKX exchange instance."""
    import ccxt

    load_env()
    ex = ccxt.okx({
        "apiKey": os.environ.get("OKX_API_KEY") or os.environ.get("OKX_APIKEY"),
        "secret": os.environ.get("OKX_SECRET") or os.environ.get("OKX_API_SECRET"),
        "password": os.environ.get("OKX_PASSPHRASE"),
        "enableRateLimit": True,
    })
    ex.load_markets()
    return ex


def to_ccxt_symbol(inst_id: str) -> str:
    """PEPE-USDT-SWAP -> PEPE/USDT:USDT"""
    base = inst_id.split("-")[0]
    return f"{base}/USDT:USDT"


def round_sz(ex, inst_id, sz: float) -> float:
    """Round order size down to the exchange lot size."""
    market = ex.market(inst_id) if inst_id in ex.markets else None
    if market is None:
        ccxt_sym = to_ccxt_symbol(inst_id)
        market = ex.market(ccxt_sym)
    lot = market.get("limits", {}).get("amount", {}).get("min") or market.get("precision", {}).get("amount", 1)
    return max(lot, int(sz / lot) * lot) if lot else sz


def fetch_indicators(ex, inst_id):
    """Latest RSI(14) / EMA50 direction / volume ratio for an instId.

    Drops the unfinished candle so ratios are not distorted.
    """
    import numpy as np

    ccxt_sym = to_ccxt_symbol(inst_id)
    ohlcv = ex.fetch_ohlcv(ccxt_sym, "1h", limit=101)
    if len(ohlcv) > 1:
        ohlcv = ohlcv[:-1]  # drop unfinished candle
    closes = np.array([c[4] for c in ohlcv], dtype=float)
    vols = np.array([c[5] for c in ohlcv], dtype=float)

    # RSI(14)
    delta = np.diff(closes, prepend=closes[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    period = 14
    avg_gain = np.zeros_like(closes)
    avg_loss = np.zeros_like(closes)
    avg_gain[period] = gain[1:period + 1].mean()
    avg_loss[period] = loss[1:period + 1].mean()
    for i in range(period + 1, len(closes)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period
    rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss != 0)
    rsi = 100 - 100 / (1 + rs)

    # EMA50
    alpha = 2 / 51
    ema50 = np.full_like(closes, np.nan)
    ema50[49] = closes[:50].mean()
    for i in range(50, len(closes)):
        ema50[i] = alpha * closes[i] + (1 - alpha) * ema50[i - 1]

    # Volume MA20 (excluding the just-dropped candle)
    vol_ma20 = vols[-21:-1].mean() if len(vols) > 21 else vols.mean()

    return {
        "price": closes[-1],
        "rsi": float(rsi[-1]),
        "ema50": float(ema50[-1]),
        "ema_rising": float(ema50[-1]) > float(ema50[-25]) if len(ema50) > 25 and not np.isnan(ema50[-25]) else None,
        "vol_ratio": float(vols[-1] / vol_ma20) if vol_ma20 > 0 else None,
    }