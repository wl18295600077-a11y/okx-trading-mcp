"""
RSI 策略回测引擎 — 针对 XWANG 的 PEPE/SATS 实盘策略
====================================================
模拟用户实际交易方式：RSI 超卖做多/超买做空，10x 杠杆，$5 保证金，固定 SL/TP

支持：
  - 入场过滤器：EMA50 趋势过滤 + 成交量(>20日均量×因子) 过滤
  - 出场模式：
      fixed  : 固定 SL/TP（用户当前方式）
      ladder : 阶梯出场（TP1 平 60% + 剩余追踪止盈 + 最大持仓时间强平）
  - 数据缓存：首次拉取存 CSV，之后直接用

用法:
  python backtest_rsi.py --symbol PEPE --mode fixed
  python backtest_rsi.py --symbol SATS --mode ladder --filters both
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

SYMBOLS = {
    "PEPE": ("PEPE/USDT:USDT", "PEPE-USDT-SWAP"),
    "SATS": ("SATS/USDT:USDT", "SATS-USDT-SWAP"),
}


# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------
def fetch_ohlcv(symbol: str, months: int = 6) -> list:
    """从 OKX 拉取 1H K线并缓存到 CSV"""
    import ccxt

    os.makedirs(DATA_DIR, exist_ok=True)
    safe_name = symbol.replace("/", "_").replace(":", "_")
    csv_path = os.path.join(DATA_DIR, f"{safe_name}_1h_{months}m.csv")

    if os.path.exists(csv_path):
        print(f"📂 使用缓存数据: {csv_path}")
        with open(csv_path, "r") as f:
            reader = csv.reader(f)
            return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in reader]

    ex = ccxt.okx({"enableRateLimit": True})
    ex.load_markets()

    now = datetime.now(timezone.utc)
    since = ex.parse8601(f"{now.year}-01-01T00:00:00Z") if months >= 6 else int(now.timestamp() * 1000) - months * 30 * 24 * 3600 * 1000

    all_ohlcv = []
    while True:
        batch = ex.fetch_ohlcv(symbol, "1h", since=since, limit=300)
        if not batch:
            break
        all_ohlcv.extend(batch)
        if len(batch) < 300:
            break
        since = batch[-1][0] + 3600 * 1000
        time.sleep(0.15)

    # 去重并按时间排序
    seen = set()
    dedup = []
    for c in all_ohlcv:
        if c[0] not in seen:
            seen.add(c[0])
            dedup.append(c)
    dedup.sort(key=lambda x: x[0])

    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(dedup)

    print(f"✅ 拉取 {symbol} 1H K线: {len(dedup)} 根 -> {csv_path}")
    return dedup


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(closes, prepend=closes[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.zeros_like(closes)
    avg_loss = np.zeros_like(closes)
    avg_gain[period] = gain[1 : period + 1].mean()
    avg_loss[period] = loss[1 : period + 1].mean()
    for i in range(period + 1, len(closes)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period
    rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss != 0)
    return 100 - 100 / (1 + rs)


def ema(values: np.ndarray, period: int) -> np.ndarray:
    out = np.zeros_like(values)
    out[:period] = np.nan
    alpha = 2 / (period + 1)
    out[period] = values[: period + 1].mean()
    for i in range(period + 1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


# ---------------------------------------------------------------------------
# 回测引擎
# ---------------------------------------------------------------------------
class RsiBacktest:
    def __init__(self, ohlcv: list, params: dict):
        self.ohlcv = np.array(ohlcv)
        self.p = params
        self.closes = self.ohlcv[:, 4]
        self.opens = self.ohlcv[:, 1]
        self.highs = self.ohlcv[:, 2]
        self.lows = self.ohlcv[:, 3]
        self.volumes = self.ohlcv[:, 5]

        self.rsi_vals = rsi(self.closes, self.p.get("rsi_period", 14))
        self.ema50 = ema(self.closes, 50)
        # 20日均量
        self.vol_ma20 = np.full_like(self.volumes, np.nan)
        for i in range(20, len(self.volumes)):
            self.vol_ma20[i] = self.volumes[i - 20 : i].mean()

    def _signal_at(self, i: int):
        """在 bar i 收盘时判定信号（下一根开盘入场，避免未来函数）"""
        r = self.rsi_vals[i]
        if np.isnan(r):
            return None

        long_ok = r < self.p["rsi_low"]
        short_ok = r > self.p["rsi_high"]

        # EMA50 方向过滤：EMA50 上升趋势中允许超卖回调做多（顺大势逆小势），
        # 下降趋势中允许超买反弹做空。用 ema50 与 24h 前比较判断方向。
        if self.p.get("use_ema_filter") and i >= 24 and not np.isnan(self.ema50[i]):
            ema_now = self.ema50[i]
            ema_prev = self.ema50[i - 24]
            if not np.isnan(ema_prev):
                ema_rising = ema_now > ema_prev
                long_ok = long_ok and ema_rising
                short_ok = short_ok and not ema_rising

        # 成交量过滤：入场要求成交量 > 20日均量 × 因子
        if self.p.get("use_vol_filter") and not np.isnan(self.vol_ma20[i]):
            long_ok = long_ok and self.volumes[i] > self.vol_ma20[i] * self.p.get("vol_factor", 1.3)
            short_ok = short_ok and self.volumes[i] > self.vol_ma20[i] * self.p.get("vol_factor", 1.3)

        if long_ok:
            return "long"
        if short_ok:
            return "short"
        return None

    def run(self) -> dict:
        p = self.p
        leverage = p.get("leverage", 10)
        margin = p.get("margin_usd", 5.0)
        notional = margin * leverage
        mode = p.get("exit_mode", "fixed")

        trades = []
        position = None  # {side, entry_idx, entry_price, size_frac, tp1_hit, peak}
        cooldown_until = 0

        for i in range(60, len(self.ohlcv)):
            # ---- 持仓管理 ----
            if position:
                side = position["side"]
                entry = position["entry_price"]
                high, low = self.highs[i], self.lows[i]

                exit_price = None
                exit_reason = None
                sl_price = entry * (1 - p["sl_pct"]) if side == "long" else entry * (1 + p["sl_pct"])

                # 止损（优先于止盈判定：同根K线先触SL）
                if side == "long" and low <= sl_price:
                    exit_price, exit_reason = sl_price, "SL"
                elif side == "short" and high >= sl_price:
                    exit_price, exit_reason = sl_price, "SL"

                if exit_price is None:
                    if mode == "fixed":
                        tp_price = entry * (1 + p["tp_pct"]) if side == "long" else entry * (1 - p["tp_pct"])
                        if side == "long" and high >= tp_price:
                            exit_price, exit_reason = tp_price, "TP"
                        elif side == "short" and low <= tp_price:
                            exit_price, exit_reason = tp_price, "TP"
                    elif mode == "ladder":
                        tp1 = entry * (1 + p["tp1_pct"]) if side == "long" else entry * (1 - p["tp1_pct"])
                        # 第一级止盈：平 60%
                        if not position.get("tp1_hit"):
                            if side == "long" and high >= tp1:
                                position["tp1_hit"] = True
                                position["size_frac"] = 1 - p.get("tp1_close", 0.6)
                                # 记录第一级已实现盈亏
                                trades.append(self._make_trade(position, tp1, i, "TP1", fraction=p.get("tp1_close", 0.6)))
                            elif side == "short" and low <= tp1:
                                position["tp1_hit"] = True
                                position["size_frac"] = 1 - p.get("tp1_close", 0.6)
                                trades.append(self._make_trade(position, tp1, i, "TP1", fraction=p.get("tp1_close", 0.6)))
                        # 剩余仓位：追踪止盈（从最高/最低点回撤 trail_pct）
                        if position.get("tp1_hit"):
                            trail = p.get("trail_pct", 0.03)
                            if side == "long":
                                peak = max(position.get("peak", entry), high)
                                position["peak"] = peak
                                if high >= peak:  # 更新峰值
                                    pass
                                if low <= peak * (1 - trail):
                                    exit_price, exit_reason = peak * (1 - trail), "TRAIL"
                            else:
                                trough = min(position.get("trough", entry), low)
                                position["trough"] = trough
                                if high <= trough:  # 更新谷值
                                    pass
                                if high >= trough * (1 + trail):
                                    exit_price, exit_reason = trough * (1 + trail), "TRAIL"

                # 最大持仓时间强平（12h = 12根）
                if exit_price is None and (i - position["entry_idx"]) >= p.get("max_hold", 12):
                    exit_price, exit_reason = self.closes[i], "TIME"

                if exit_price is not None:
                    trades.append(self._make_trade(position, exit_price, i, exit_reason, fraction=position["size_frac"]))
                    position = None
                    cooldown_until = i + p.get("cooldown", 4)

            # ---- 开仓 ----
            if position is None and i >= cooldown_until:
                sig = self._signal_at(i)
                if sig:
                    entry_price = self.opens[i + 1] if i + 1 < len(self.ohlcv) else None
                    if entry_price:
                        position = {
                            "side": sig,
                            "entry_idx": i + 1,
                            "entry_price": entry_price,
                            "size_frac": 1.0,
                            "tp1_hit": False,
                            "peak": entry_price if sig == "long" else None,
                            "trough": entry_price if sig == "short" else None,
                            "notional": notional,
                        }

        # 期末强平
        if position:
            trades.append(self._make_trade(position, self.closes[-1], len(self.ohlcv) - 1, "END", fraction=position["size_frac"]))

        return self._report(trades, margin)

    def _make_trade(self, pos, exit_price, exit_idx, reason, fraction):
        entry, side = pos["entry_price"], pos["side"]
        price_pct = (exit_price - entry) / entry if side == "long" else (entry - exit_price) / entry
        # 交易成本: 手续费(taker 0.05%双边) + 滑点(市价单双边)
        # 按名义价值百分比扣减: cost = (fee + slip) * 2 (开+平)
        fee_rate = self.p.get("fee_rate", 0.0005)   # OKX USDT永续 taker 0.05%
        slippage = self.p.get("slippage", 0.0005)   # meme币市价单滑点 0.05%
        cost_pct = (fee_rate + slippage) * 2
        price_pct_net = price_pct - cost_pct
        # 杠杆放大后的保证金盈亏
        margin_pnl_pct = price_pct_net * self.p.get("leverage", 10)
        return {
            "side": side,
            "entry_idx": pos["entry_idx"],
            "exit_idx": exit_idx,
            "entry_price": entry,
            "exit_price": exit_price,
            "price_pct": price_pct,
            "cost_pct": cost_pct,
            "margin_pnl_pct": margin_pnl_pct,
            "reason": reason,
            "fraction": fraction,
            "duration": exit_idx - pos["entry_idx"],
        }

    def _report(self, trades, margin):
        if not trades:
            return {"trades": 0, "win_rate": 0, "total_pnl_usd": 0, "profit_factor": 0,
                    "max_drawdown": 0, "avg_hold": 0, "total_return_pct": 0, "details": []}

        pnl_usd = [t["margin_pnl_pct"] / 100 * margin * t["fraction"] for t in trades]
        total = sum(pnl_usd)
        wins = sum(1 for x in pnl_usd if x > 0)
        losses = sum(1 for x in pnl_usd if x <= 0)
        gross_profit = sum(x for x in pnl_usd if x > 0)
        gross_loss = abs(sum(x for x in pnl_usd if x < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # 最大回撤（按累计盈亏算）
        equity = 0
        peak = 0
        mdd = 0
        for x in pnl_usd:
            equity += x
            peak = max(peak, equity)
            mdd = max(mdd, (peak - equity))

        return {
            "trades": len(trades),
            "win_rate": wins / len(trades) if trades else 0,
            "total_pnl_usd": round(total, 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
            "max_drawdown": round(mdd, 2),
            "avg_hold": round(sum(t["duration"] for t in trades) / len(trades), 1),
            "total_return_pct": round(total / margin * 100, 1) if margin else 0,
            "details": trades,
        }


# ---------------------------------------------------------------------------
# 参数集
# ---------------------------------------------------------------------------
def param_sets():
    """四组对比参数：基线 / +过滤器 / +阶梯出场 / 全开"""
    base = dict(
        rsi_period=14, rsi_low=30, rsi_high=70,
        sl_pct=0.05, tp_pct=0.08,
        use_ema_filter=False, use_vol_filter=False, vol_factor=1.3,
        exit_mode="fixed",
        leverage=10, margin_usd=5.0, cooldown=4, max_hold=999,
    )
    filtered = dict(base, use_ema_filter=True, use_vol_filter=True)
    ladder = dict(base, exit_mode="ladder", tp1_pct=0.08, tp1_close=0.6,
                  trail_pct=0.03, max_hold=12)
    full = dict(ladder, use_ema_filter=True, use_vol_filter=True)
    return [
        ("A 基线 fixed", base),
        ("B 基线+过滤器 fixed", filtered),
        ("C 基线+阶梯 ladder", ladder),
        ("D 全部 ladder", full),
    ]


def fmt_report(r: dict) -> str:
    pf = f"{r['profit_factor']:.2f}" if r["profit_factor"] is not None else "∞"
    return (f"交易 {r['trades']:>4} | 胜率 {r['win_rate']*100:5.1f}% | "
            f"总盈亏 ${r['total_pnl_usd']:>8.2f} | PF {pf:>5} | "
            f"回撤 ${r['max_drawdown']:>6.2f} | 均持仓 {r['avg_hold']:>5.1f}h | "
            f"收益率 {r['total_return_pct']:>7.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", choices=["PEPE", "SATS", "BOTH"], default="PEPE")
    ap.add_argument("--mode", choices=["fixed", "ladder"], default=None,
                    help="只跑某种出场模式；缺省跑全部4组")
    ap.add_argument("--months", type=int, default=6)
    args = ap.parse_args()

    symbols = ["PEPE", "SATS"] if args.symbol == "BOTH" else [args.symbol]
    all_results = {}

    for sym in symbols:
        print(f"\n{'='*76}\n📊 {sym} 回测\n{'='*76}")
        ccxt_sym, _ = SYMBOLS[sym]
        ohlcv = fetch_ohlcv(ccxt_sym, args.months)
        if len(ohlcv) < 300:
            print(f"⚠️ {sym} 数据不足: {len(ohlcv)} 根")
            continue

        sets = param_sets()
        if args.mode:
            sets = [s for s in sets if s[0].endswith(args.mode)]

        results = []
        for name, params in sets:
            bt = RsiBacktest(ohlcv, params)
            r = bt.run()
            print(f"  {name:28s} {fmt_report(r)}")
            results.append((name, r))
        all_results[sym] = results

        # 保存明细
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, f"backtest_{sym}_{args.mode or 'all'}.json"), "w") as f:
            json.dump({name: r for name, r in results}, f, indent=2, default=str)

    print(f"\n✅ 完成，结果已保存到 {DATA_DIR}/")


if __name__ == "__main__":
    main()