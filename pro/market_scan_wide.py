# -*- coding: utf-8 -*-
"""全市场机会扫描 — OKX 全部 USDT 永续，按24h成交额取前N个，逐对算 RSI/EMA50/量比，筛超卖/超买候选。
用法: F:/AI/Python312/python.exe market_scan_wide.py [--top 60] [--rsi-low 35] [--rsi-high 65]
"""
import sys, os, io, argparse
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from okx_utils import get_exchange, fetch_indicators


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60, help="按24h成交额取前N个对")
    ap.add_argument("--rsi-low", type=float, default=35, help="做多候选 RSI 阈值")
    ap.add_argument("--rsi-high", type=float, default=65, help="做空候选 RSI 阈值")
    args = ap.parse_args()

    ex = get_exchange()

    # 1. 全市场 tickers → USDT 永续 → 按成交额排序
    print("拉取全市场 SWAP tickers ...")
    tickers = ex.fetch_tickers(params={"instType": "SWAP"})
    swaps = {s: t for s, t in tickers.items() if s.endswith("/USDT:USDT")}
    # OKX 不返回 quoteVolume，用 baseVolume × last 估算 24h 成交额
    def est_qv(t):
        return (t.get("baseVolume") or 0) * (t.get("last") or 0)
    ranked = sorted(swaps.items(), key=lambda kv: est_qv(kv[1]), reverse=True)
    top = [(sym, est_qv(t), t.get("percentage")) for sym, t in ranked[:args.top]]
    print(f"共 {len(swaps)} 个 USDT 永续，扫描成交额前 {len(top)} 名\n")

    # 2. 逐对计算指标
    longs, shorts, watch = [], [], []
    fails = []
    for sym, qv, pct in top:
        inst_id = sym.replace("/USDT:USDT", "-USDT-SWAP")
        try:
            ind = fetch_indicators(ex, inst_id)
            rsi, ema_rising, vr = ind["rsi"], ind["ema_rising"], ind["vol_ratio"] or 0
            price = ind["price"]
            rec = (inst_id, rsi, ema_rising, vr, price, qv, pct)
            if rsi < args.rsi_low:
                longs.append(rec)
            elif rsi > args.rsi_high:
                shorts.append(rec)
            elif (args.rsi_low <= rsi < args.rsi_low + 5) or (args.rsi_high - 5 < rsi <= args.rsi_high):
                watch.append(rec)
        except Exception as e:
            fails.append((inst_id, str(e)[:80]))

    def fmt_p(p):
        s = f"{p:.10f}".rstrip("0").rstrip(".")
        return s if len(s) <= 13 else f"{p:.3e}"

    def dump(title, recs):
        print(f"\n{'='*72}\n{title} ({len(recs)})\n{'='*72}")
        if not recs:
            print("  无")
            return
        print(f"{'币种':<14}{'RSI':<7}{'EMA50':<7}{'量比':<7}{'价格':<15}{'24h%':<8}{'成交额'}")
        for inst_id, rsi, er, vr, price, qv, pct in recs:
            pct_s = f"{pct:+.1f}%" if pct is not None else "-"
            qv_s = f"{qv/1e6:.0f}M" if qv > 1e6 else f"{qv/1e3:.0f}K"
            print(f"{inst_id:<14}{rsi:<7.1f}{'↑' if er else '↓' if er is not None else '-':<7}"
                  f"{vr:<7.2f}{fmt_p(price):<15}{pct_s:<8}{qv_s}")

    dump(f"🟢 做多候选 (RSI < {args.rsi_low})", sorted(longs, key=lambda r: r[1]))
    dump(f"🔴 做空候选 (RSI > {args.rsi_high})", sorted(shorts, key=lambda r: -r[1]))
    dump(f"🟡 接近阈值 (观察名单)", sorted(watch, key=lambda r: min(abs(r[1]-35), abs(r[1]-65))))

    if fails:
        print(f"\n⚠️ {len(fails)} 个查询失败: {[f[0] for f in fails[:8]]}")


if __name__ == "__main__":
    main()