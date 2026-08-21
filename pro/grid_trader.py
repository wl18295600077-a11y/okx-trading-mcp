"""
网格交易 — 横盘市场低买高卖
============================
标 的: SATS-USDT-SWAP (只做多网格, 永续做空网格坑多, 不碰)
区 间: 9.97e-09 ~ 1.09e-08 (7天震荡区间)
格 数: 4 格, 每格 ~2.3%
名义:  $12.5/格 = $5 保证金 x 10x / 4
规则:
  价格跌到某格线 -> 市价开多 (该格名义)
  价格涨到上一格线 -> 市价平多 (吃一格差价)
  跌破区间下沿 -2% -> 全部止损
  涨破区间上沿 -> 全部止盈 (等回落再进)
用法:
  python grid_trader.py            # 前台跑
  python grid_trader.py --dry-run  # 只打印不开单
"""
import sys, os, time, argparse, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from execute_trade import get_exchange, to_ccxt_symbol, round_sz, set_leverage, align_tick, fmt_px
import ledger

INST_ID = "SATS-USDT-SWAP"
CCXT_SYM = "SATS/USDT:USDT"

# 网格参数 (SATS 7天区间)
LOW = 9.97e-09
HIGH = 1.09e-08
N_GRID = 4
GRID_STEP = (HIGH - LOW) / N_GRID      # ~2.3%
LEVERAGE = 10
MARGIN_PER_GRID = 1.25                 # $5 / 4格
NOMINAL_PER_GRID = MARGIN_PER_GRID * LEVERAGE  # $12.5
POLL_SEC = 20

def grid_lines():
    return [LOW + i * GRID_STEP for i in range(N_GRID + 1)]

def get_price(ex):
    t = ex.fetch_ticker(CCXT_SYM)
    return float(t["last"])

def get_positions(ex, inst_id):
    """返回 {long: sz, short: sz}"""
    pos = ex.fetch_positions([CCXT_SYM])
    out = {"long": 0.0, "short": 0.0}
    for p in pos:
        amt = float(p.get("contracts") or 0)
        if amt == 0:
            continue
        side = p.get("side") or p.get("posSide")
        out[side] += abs(amt)
    return out

def get_size_for_notional(ex, notional_usd):
    """按名义金额换算合约张数 (SATS 面值 1000 张=1币? 直接取 market precision 兜底)"""
    m = ex.market(CCXT_SYM)
    # OKX 张: contractSize
    cs = float(m.get("info", {}).get("ctVal", 1))
    price = get_price(ex)
    contracts = notional_usd / (cs * price)
    return round_sz(ex, INST_ID, contracts)

def cancel_pending_algos(ex, inst_id):
    """平仓后清理驻留的 SL/TP 废单"""
    try:
        res = ex.private_get_trade_orders_algo_pending({"instId": inst_id, "ordType": "conditional"})
        algos = res.get("data", [])
        to_cancel = [{"instId": inst_id, "algoId": a["algoId"]} for a in algos]
        if to_cancel:
            ex.private_post_trade_cancel_algos(to_cancel)  # ⚠️ body 必须数组, dict→50002
            print(f"  🧹 清理废单 {len(to_cancel)} 个")
    except Exception as e:
        print(f"  ⚠️ 清理废单失败: {e}")

def open_grid_long(ex, inst_id, sz, price):
    """开多 + 挂 SL(区间下沿-2%) + TP(上一格线)"""
    pos_side = "long"
    trade_side = "buy"
    sl_px = align_tick(ex, inst_id, LOW * 0.98)
    tp_px = align_tick(ex, inst_id, price + GRID_STEP)
    attach = [{
        "slTriggerPx": fmt_px(ex, inst_id, sl_px), "slOrdPx": "-1",
        "tpTriggerPx": fmt_px(ex, inst_id, tp_px), "tpOrdPx": "-1",
        "sz": str(sz), "side": trade_side, "posSide": pos_side,
    }]
    order = ex.create_order(CCXT_SYM, "market", trade_side, sz, None, {
        "attachAlgoOrds": attach, "tdMode": "isolated", "posSide": pos_side,
    })
    avg = float(order.get("average") or price)
    print(f"  🟢 开多 {sz}张 @ {avg:.10f}  (SL {sl_px:.2e} / TP {tp_px:.2e})")
    try:
        ct_val = float(ex.market(CCXT_SYM).get("info", {}).get("ctVal", 1))
        ledger.record_open(INST_ID, "long", "grid", sz, ct_val, avg,
                           MARGIN_PER_GRID, LEVERAGE, sl_px=sl_px, tp_px=tp_px,
                           fee=0.0, note="grid")
    except Exception as e:
        print(f"  ⚠️ 记账失败: {e}")
    return avg

def close_grid_long(ex, inst_id, sz, reason="take_profit"):
    """平多 + 清理废单 + 记账"""
    pos_side = "long"
    cancel_pending_algos(ex, inst_id)
    order = ex.create_order(CCXT_SYM, "market", "sell", sz, None,
                            {"tdMode": "isolated", "posSide": pos_side, "reduceOnly": "true"})
    avg = float(order.get("average") or 0)
    print(f"  🔴 平多 {sz}张 @ {avg:.10f} ({reason})")
    try:
        ledger.record_close(INST_ID, exit_px=avg, close_reason=reason, note="grid")
    except Exception as e:
        print(f"  ⚠️ 记账失败: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ex = get_exchange()
    set_leverage(ex, INST_ID, LEVERAGE)
    lines = grid_lines()
    print(f"=== SATS 网格 {N_GRID}格 | 区间 {LOW:.2e}~{HIGH:.2e} | 每格 {GRID_STEP/HIGH*100:.1f}% | "
          f"名义 ${NOMINAL_PER_GRID:.1f}/格 | {'DRY-RUN' if args.dry_run else 'LIVE'} ===")
    for i, l in enumerate(lines):
        print(f"  格线{i}: {l:.2e}")
    print()

    # 初始状态: 看价格在哪个网格区间
    price = get_price(ex)
    print(f"初始价格: {price:.10f}")
    pos = get_positions(ex, INST_ID)
    print(f"当前持仓: {pos}")

    # 穿越检测状态: 记录上一轮所在格, 价格跌穿格线(格号变小)才开多
    last_grid = max(0, min(N_GRID - 1, int((price - LOW) / GRID_STEP)))
    grid_open_idx = None  # 开仓时的格号, 涨穿上一格线(格号变大)则平多

    while True:
        try:
            price = get_price(ex)
            pos = get_positions(ex, INST_ID)
            ts = time.strftime("%H:%M:%S")
            grid_idx = int((price - LOW) / GRID_STEP)
            grid_idx = max(0, min(N_GRID - 1, grid_idx))
            line_below = lines[grid_idx]
            line_above = lines[grid_idx + 1]
            print(f"[{ts}] 价格 {price:.4e} | 格[{grid_idx}] {line_below:.2e}~{line_above:.2e} | "
                  f"持仓 {pos['long']:.1f}张", flush=True)

            long_sz = pos["long"]

            # 空仓: 清废单 + 等价格跌穿格线开多
            if long_sz == 0:
                if last_grid > grid_idx:
                    # 价格从高格跌穿到低格 -> 开多
                    sz = get_size_for_notional(ex, NOMINAL_PER_GRID)
                    if sz <= 0:
                        print("  ⚠️ 张数计算异常, 跳过")
                    elif args.dry_run:
                        print(f"  [DRY] 跌穿格线, 开多 {sz}张 @ {price:.10f}")
                        grid_open_idx = grid_idx
                    else:
                        open_grid_long(ex, INST_ID, sz, price)
                        grid_open_idx = grid_idx
                # 价格涨破区间上沿: 空仓不追, 等回落
            else:
                # 持仓: 涨穿上一格线 -> 平多吃差价; 跌破止损线 -> 止损
                if grid_open_idx is not None and grid_idx > grid_open_idx:
                    if args.dry_run:
                        print(f"  [DRY] 涨穿上一格, 平多 {long_sz}张 @ {price:.10f} (吃一格差价)")
                    else:
                        close_grid_long(ex, INST_ID, long_sz, "grid_tp")
                    grid_open_idx = None
                elif price <= LOW * 0.98:
                    if args.dry_run:
                        print(f"  [DRY] 止损平多 {long_sz}张 (破区间下沿)")
                    else:
                        close_grid_long(ex, INST_ID, long_sz, "grid_sl")
                    grid_open_idx = None

            last_grid = grid_idx
        except Exception as e:
            print(f"  ⚠️ 循环异常: {e}", flush=True)
        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
