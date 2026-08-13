"""
OKX 交易执行工具 — 集成改动 3/4/6
==================================
功能:
  [改动6] ladder 模式: 开仓时挂 SL(全量) + TP1(60%) + 追踪止损(40%) + 12h 强平提示
  [改动4] 入场过滤器: RSI 阈值 + EMA50 方向 + 成交量确认 (可开关, 默认按回测最优参数)
  [改动3] 平仓后自动清理该 instId 全部 pending algo 废单

用法:
  # 开仓 (ladder 阶梯出场)
  python execute_trade.py open --symbol PEPE-USDT-SWAP --side long --margin 5 --mode ladder
  # 开仓 (fixed 固定 SL/TP)
  python execute_trade.py open --symbol SATS-USDT-SWAP --side short --margin 5 --mode fixed
  # 平仓 + 自动清废单
  python execute_trade.py close --symbol PEPE-USDT-SWAP
  # 查询持仓 + 挂单
  python execute_trade.py status --symbol PEPE-USDT-SWAP
  # 清理废单（手动）
  python execute_trade.py cleanup --symbol PEPE-USDT-SWAP

回测最优参数（2026-01 ~ 07, 1H K线）:
  PEPE ladder: RSI 35/75, SL 5%, TP1 8%, 追踪 3%, 强平 16h  -> PF 1.44, 胜率 58%
  PEPE fixed : RSI 35/65, SL 4%, TP 8%                      -> PF 1.49
  SATS ladder: RSI 25/65, SL 5%, TP1 10%, 追踪 3%, 强平 12h -> PF 1.33, 胜率 59%
  SATS fixed : RSI 35/65, SL 4%, TP 15%                     -> PF 2.03
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

# 兼容两种 .env 位置
for env_path in [os.path.expanduser("~/.hermes/.env"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")]:
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

import ccxt
import ledger

# 各币种回测最优参数 (2026-08-04 walk_forward 验证更新)
# verified: True = 样本外验证通过(可信) | False = 过拟合风险(谨慎, 建议降杠杆)
# 验证详情见 data/walkforward_*.json 和 walk_forward.py
PARAMS = {
    "PEPE": {
        # ⚠️ 过拟合风险: 训练段PF1.40 → 验证段PF0.83(亏损)。5-7月行情与1-4月差异大。
        # 建议: 用 5x 杠杆替代默认 10x，或暂停该币种。
        "ladder": dict(rsi_low=35, rsi_high=75, sl_pct=0.05, tp1_pct=0.08,
                       trail_pct=0.03, max_hold_h=16, tp1_close=0.6,
                       verified=False, leverage_hint=5),
        "fixed": dict(rsi_low=35, rsi_high=65, sl_pct=0.04, tp_pct=0.08,
                      verified=False, leverage_hint=5),
    },
    "SATS": {
        # ✅ 验证通过: fixed 训练PF1.83→验证PF5.87; ladder 训练PF1.30→验证PF2.10
        "ladder": dict(rsi_low=25, rsi_high=65, sl_pct=0.05, tp1_pct=0.10,
                       trail_pct=0.03, max_hold_h=12, tp1_close=0.6,
                       verified=True),
        "fixed": dict(rsi_low=35, rsi_high=65, sl_pct=0.04, tp_pct=0.15,
                      verified=True),
    },
}

# 默认杠杆；PEPE(未验证) 强制降到 5x
LEVERAGE = 10


def get_exchange():
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
    """按 lotSz 向下取整"""
    market = ex.market(inst_id) if inst_id in ex.markets else None
    if market is None:
        ccxt_sym = to_ccxt_symbol(inst_id)
        market = ex.market(ccxt_sym)
    lot = market.get("limits", {}).get("amount", {}).get("min") or market.get("precision", {}).get("amount", 1)
    return max(lot, int(sz / lot) * lot) if lot else sz


def fetch_indicators(ex, inst_id):
    """获取最新 RSI(14) / EMA50 方向 / 成交量比，用于入场过滤器
    注意: 丢弃最后一根未走完的K线，只用已完成的K线计算，避免量比失真"""
    import numpy as np
    ccxt_sym = to_ccxt_symbol(inst_id)
    ohlcv = ex.fetch_ohlcv(ccxt_sym, "1h", limit=101)
    if len(ohlcv) > 1:
        ohlcv = ohlcv[:-1]  # 去掉当前未完成的一根
    closes = np.array([c[4] for c in ohlcv], dtype=float)
    vols = np.array([c[5] for c in ohlcv], dtype=float)

    # RSI
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

    # 成交量 MA20
    vol_ma20 = vols[-21:-1].mean() if len(vols) > 21 else vols.mean()

    return {
        "price": closes[-1],
        "rsi": float(rsi[-1]),
        "ema50": float(ema50[-1]),
        "ema_rising": float(ema50[-1]) > float(ema50[-25]) if len(ema50) > 25 and not np.isnan(ema50[-25]) else None,
        "vol_ratio": float(vols[-1] / vol_ma20) if vol_ma20 > 0 else None,
    }


def memory_check(symbol: str, side: str, verbose=True):
    """📋 交易前记忆回查（学 tradememory 的记忆层概念）
    开仓前自动查账本: 同币同向历史战绩, 避免重复犯错。
    只读 ledger.db, 不阻塞开仓, 提供警示信息。
    """
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db")
    if not os.path.exists(db_path):
        return None
    try:
        import sqlite3
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(
            """SELECT symbol, side, mode, entry_px, exit_px, pnl, pnl_pct, close_reason,
                      ts_open, ts_close
               FROM trades WHERE symbol=? AND side=? AND status='closed'
               ORDER BY ts_close DESC""",
            (symbol, side))
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        print(f"  ⚠️ 记忆查询失败: {e}")
        return None

    if not rows:
        if verbose:
            print(f"  📋 记忆回查: {symbol} {side} 无历史记录（首次开此方向）")
        return None

    pnls = [r[5] for r in rows if r[5] is not None]
    n = len(rows)
    wr = len([p for p in pnls if p > 0]) / n if n else 0
    avg = sum(pnls) / len(pnls) if pnls else 0
    total = sum(pnls) if pnls else 0
    print(f"  📋 记忆回查 {symbol} {side}: 历史 {n} 笔 | 胜率 {wr:.0%} | "
          f"平均 {avg:+.4f} | 累计 {total:+.4f} USDT")
    for r in rows[:3]:
        ts_c = (r[9] or "")[:16]
        print(f"     {r[8][:10]} {'✅' if (r[5] or 0) > 0 else '❌'} "
              f"模式={r[2]} 盈亏={r[6]:+.2f}% 原因={r[7]} 平仓于 {ts_c}")
    if n >= 2 and wr < 0.5:
        print(f"  ⚠️  同向胜率 {wr:.0%} < 50%，建议谨慎或减仓！(--skip-memory 可忽略)")
    if rows and (rows[0][5] or 0) < 0:
        print(f"  ⚠️  最近一笔同向亏损 {rows[0][6]:+.2f}%，注意风险")
    return rows


def check_filters(ex, inst_id, side: str, params: dict, verbose=True):
    """改动4: 入场过滤器 — RSI 阈值 + EMA50 方向 + 成交量确认"""
    ind = fetch_indicators(ex, inst_id)
    if verbose:
        print(f"  指标: RSI={ind['rsi']:.1f} | 价格={ind['price']:.10f} | "
              f"EMA50方向={'上升' if ind['ema_rising'] else '下降' if ind['ema_rising'] is not None else 'N/A'} | "
              f"量比={ind['vol_ratio']:.2f}x" if ind['vol_ratio'] else "")

    checks = []
    # 1. RSI 阈值
    if side == "long":
        ok = ind["rsi"] < params.get("rsi_low", 35)
        checks.append((ok, f"RSI {ind['rsi']:.1f} < {params.get('rsi_low', 35)} (超卖做多)"))
    else:
        ok = ind["rsi"] > params.get("rsi_high", 65)
        checks.append((ok, f"RSI {ind['rsi']:.1f} > {params.get('rsi_high', 65)} (超买做空)"))

    # 2. EMA50 方向（顺大势逆小势）
    if ind["ema_rising"] is not None:
        if side == "long":
            ok = ind["ema_rising"]
            checks.append((ok, f"EMA50 上升 ({ind['ema50']:.10f})"))
        else:
            ok = not ind["ema_rising"]
            checks.append((ok, f"EMA50 下降 ({ind['ema50']:.10f})"))

    # 3. 成交量确认
    if ind["vol_ratio"] is not None:
        ok = ind["vol_ratio"] >= 1.0
        checks.append((ok, f"量比 {ind['vol_ratio']:.2f}x"))

    all_ok = all(ok for ok, _ in checks)
    if verbose:
        print("  " + " | ".join(("✅ " if ok else "❌ ") + desc for ok, desc in checks))
    return all_ok, ind


def set_leverage(ex, inst_id, lev=LEVERAGE):
    try:
        # OKX 双向持仓(hedge)模式下 set_leverage 必须带 posSide，否则报 51000 Parameter posSide error
        for pos_side in ["long", "short"]:
            ex.private_post_account_set_leverage({
                "instId": inst_id, "lever": str(lev), "mgnMode": "isolated", "posSide": pos_side
            })
        print(f"  ✅ 杠杆已设为 {lev}x isolated (long+short)")
    except Exception as e:
        print(f"  ⚠️ 杠杆设置: {e}")


def place_ladder_orders(ex, inst_id, side, entry_price, sz, params):
    """改动6: 阶梯出场 — 开仓附带 SL(全量) + TP1(60%)，剩余仓位追踪止损"""
    pos_side = "long" if side == "long" else "short"
    trade_side = "buy" if side == "long" else "sell"

    sl_px = entry_price * (1 - params["sl_pct"]) if side == "long" else entry_price * (1 + params["sl_pct"])
    tp1_px = entry_price * (1 + params["tp1_pct"]) if side == "long" else entry_price * (1 - params["tp1_pct"])
    sz_tp1 = round_sz(ex, inst_id, sz * params["tp1_close"])
    sz_remain = max(0, round_sz(ex, inst_id, sz - sz_tp1))  # 剩余=总数-TP1，保证全覆盖

    print(f"\n  🛡️ 阶梯出场 (entry={entry_price:.10f}):")
    print(f"    SL   {params['sl_pct']:.0%} -> {sl_px:.10f} (全量 {sz}张)")
    print(f"    TP1  +{params['tp1_pct']:.0%} -> {tp1_px:.10f} ({sz_tp1}张, 平{params['tp1_close']:.0%})")
    print(f"    追踪 {params['trail_pct']:.0%} 回撤 ({sz_remain}张剩余)")

    # 开仓时附带: SL(全量) + TP1(部分) — attachAlgoOrds 挂到开仓单上
    attach = []
    # SL 全量（一张单覆盖全部）
    attach.append({
        "slTriggerPx": str(sl_px),
        "slOrdPx": "-1",
        "sz": str(sz),
        "side": trade_side,
        "posSide": pos_side,
    })
    # TP1 部分（平 tp1_close 比例）
    attach.append({
        "tpTriggerPx": str(tp1_px),
        "tpOrdPx": "-1",
        "sz": str(sz_tp1),
        "side": trade_side,
        "posSide": pos_side,
    })
    return attach, sz_remain


def place_fixed_orders(ex, inst_id, side, entry_price, sz, params):
    """固定 SL/TP — 开仓附带 SL + TP（全量）"""
    sl_px = entry_price * (1 - params["sl_pct"]) if side == "long" else entry_price * (1 + params["sl_pct"])
    tp_px = entry_price * (1 + params["tp_pct"]) if side == "long" else entry_price * (1 - params["tp_pct"])
    trade_side = "buy" if side == "long" else "sell"
    pos_side = "long" if side == "long" else "short"
    print(f"\n  🛡️ 固定 SL/TP (entry={entry_price:.10f}):")
    print(f"    SL {params['sl_pct']:.0%} -> {sl_px:.10f} | TP +{params['tp_pct']:.0%} -> {tp_px:.10f} (全量 {sz}张)")
    attach = [{
        "slTriggerPx": str(sl_px),
        "slOrdPx": "-1",
        "tpTriggerPx": str(tp_px),
        "tpOrdPx": "-1",
        "sz": str(sz),
        "side": trade_side,
        "posSide": pos_side,
    }]
    return attach, 0


def cmd_open(args):
    ex = get_exchange()
    inst_id = args.symbol
    base = inst_id.split("-")[0]
    params = PARAMS.get(base, PARAMS["PEPE"])[args.mode]

    # 验证状态检查: 未验证币种强制降杠杆
    leverage = LEVERAGE
    if not params.get("verified", True):
        leverage = params.get("leverage_hint", 5)
        print(f"⚠️  {base} 参数未通过样本外验证(过拟合风险)")
        print(f"    已自动降杠杆 {LEVERAGE}x → {leverage}x (可用 --leverage 覆盖)")
    if args.leverage:
        leverage = args.leverage

    print(f"=== 开仓 {inst_id} {args.side} | {args.mode}模式 | 保证金 ${args.margin} | {leverage}x ===")

    # 0. 交易前记忆回查 (只读账本, 不阻塞)
    if not args.skip_memory:
        memory_check(inst_id, args.side)

    # 1. 过滤器检查
    if not args.skip_filters:
        ok, ind = check_filters(ex, inst_id, args.side, params)
        if not ok:
            print("\n❌ 入场过滤器未通过，拒绝开仓（可用 --skip-filters 强制开仓）")
            sys.exit(1)
        print("  ✅ 过滤器通过\n")
    else:
        ind = fetch_indicators(ex, inst_id)
        print(f"  (跳过过滤器) RSI={ind['rsi']:.1f}")

    # 2. 设置杠杆
    set_leverage(ex, inst_id, leverage)

    # 3. 计算张数: 张数 = 保证金×杠杆 / (价格×ctVal)
    ticker = ex.fetch_ticker(to_ccxt_symbol(inst_id))
    price = ticker["last"]
    market = ex.market(to_ccxt_symbol(inst_id))
    ct_val = market.get("contractSize", 1)
    raw_sz = args.margin * leverage / (price * ct_val)
    sz = round_sz(ex, inst_id, raw_sz)
    print(f"\n  价格={price:.10f} ctVal={ct_val} -> 张数 {raw_sz:.4f} -> {sz}")

    # 4. 开仓 + 附带 SL/TP（attachAlgoOrds 随开仓单一起提交）
    ccxt_sym = to_ccxt_symbol(inst_id)
    trade_side = "buy" if args.side == "long" else "sell"
    pos_side = "long" if args.side == "long" else "short"
    if args.mode == "ladder":
        attach, sz_remain = place_ladder_orders(ex, inst_id, args.side, price, sz, params)
    else:
        attach, sz_remain = place_fixed_orders(ex, inst_id, args.side, price, sz, params)

    order = ex.create_order(ccxt_sym, "market", trade_side, sz, None, {
        "attachAlgoOrds": attach,
        "tdMode": "isolated",
        "posSide": pos_side,
    })
    print(f"  ✅ 开仓成交: {order.get('id')} 均价 {order.get('average')}")

    # 4.1 账本记录开仓
    try:
        sl_px = tp_px = None
        if attach:
            for a in attach:
                if a.get("slTriggerPx"):
                    sl_px = float(a["slTriggerPx"])
                if a.get("tpTriggerPx"):
                    tp_px = float(a["tpTriggerPx"])
        ledger.record_open(
            symbol=inst_id, side=args.side, mode=args.mode, sz=sz, ct_val=ct_val,
            entry_px=order.get("average") or price, margin=args.margin, leverage=leverage,
            sl_px=sl_px, tp_px=tp_px,
            trail_pct=params.get("trail_pct"), max_hold_h=params.get("max_hold_h"),
            fee=float((order.get("fee") or {}).get("cost") or 0),
            filters=ind, note=f"order_id={order.get('id')}")
    except Exception as e:
        print(f"  ⚠️ 账本记录失败: {e}")

    # ladder 模式: 剩余仓位挂追踪止损（move_order_stop，回调触发）
    if args.mode == "ladder" and sz_remain > 0:
        try:
            opp_trade = "sell" if args.side == "long" else "buy"
            opp_pos = args.side  # 追踪止损平仓方向 = 原仓位方向: 开多→posSide=long/sell, 开空→posSide=short/buy
            # ⚠️ 必须用 OKX 原生 algo 接口: ccxt create_order 的 reduceOnly 是模拟实现,
            #    会被 omit 剔除 → OKX 端 reduceOnly=false → 追踪单变成"开仓单"!
            #    2026-08-10 实锤: 开多挂 sell/short 追踪单, 价格回调3%自动开了空仓(幽灵单)
            algo = ex.private_post_trade_order_algo({
                "instId": inst_id,
                "tdMode": "isolated",
                "side": opp_trade,
                "posSide": opp_pos,
                "sz": str(sz_remain),
                "ordType": "move_order_stop",
                "callbackRatio": str(params["trail_pct"] * 100),  # 回调 X% 触发
                "reduceOnly": "true",  # 显式传字符串, 走原生 API 不被 ccxt 剔除
            })
            algo_id = algo["data"][0]["algoId"]
            print(f"  ✅ 追踪止损已挂 (剩余 {sz_remain}张, 回撤 {params['trail_pct']:.0%}, algoId={algo_id})")
        except Exception as e:
            print(f"  ⚠️ 追踪止损: {e}")

    if args.mode == "ladder":
        print(f"\n  ⏰ 提醒: 剩余仓位 {params['tp1_close']*100:.0f}% 靠追踪止损保护，"
              f"{params['max_hold_h']}h 内未触发建议手动评估强平")


def cmd_close(ex=None, inst_id=None, args=None):
    """改动3: 平仓 + 自动清理该 instId 全部 algo 废单"""
    if ex is None:
        ex = get_exchange()
    if inst_id is None:
        inst_id = args.symbol

    print(f"=== 平仓 {inst_id} ===")
    ccxt_sym = to_ccxt_symbol(inst_id)

    # 1. 查当前持仓
    positions = ex.private_get_account_positions({"instType": "SWAP", "instId": inst_id})
    pos = [p for p in positions.get("data", []) if float(p.get("pos", 0)) != 0]
    if not pos:
        print("  ℹ️ 无持仓")
    else:
        for p in pos:
            side = "long" if float(p["pos"]) > 0 else "short"
            sz = abs(float(p["pos"]))
            print(f"  持仓: {side} {sz}张 均价 {p.get('avgPx')}")
            order = ex.create_order(ccxt_sym, "market", "sell" if side == "long" else "buy", sz)
            print(f"  ✅ 已市价平仓 {side} {sz}张")
            # 账本记录平仓 (盈亏自动计算, 含开平仓手续费)
            try:
                ledger.record_close(
                    symbol=inst_id,
                    exit_px=order.get("average"),
                    fee=float((order.get("fee") or {}).get("cost") or 0),
                    close_reason="manual",
                    note=f"order_id={order.get('id')}",
                )
            except Exception as e:
                print(f"  ⚠️ 账本记录失败: {e}")

    # 2. 清理该 instId 全部 pending algo 单（废单清理）
    time.sleep(1)
    pending = fetch_pending_algos(ex, inst_id)
    if not pending:
        print("  ✅ 无残留条件单")
        return

    to_cancel = [{"algoId": a["algoId"], "instId": inst_id} for a in pending]
    try:
        r = ex.private_post_trade_cancel_algos(to_cancel)
        print(f"  🧹 已清理 {len(to_cancel)} 个废单: {json.dumps(r, ensure_ascii=False)}")
    except Exception as e:
        print(f"  ⚠️ 批量清理失败: {e}，逐个重试")
        for c in to_cancel:
            try:
                ex.private_post_trade_cancel_algos([c])
            except Exception as e2:
                print(f"    取消 {c['algoId']} 失败: {e2}")


def fetch_pending_algos(ex, inst_id):
    """查询全部 pending algo 单（conditional 条件单 + move_order_stop 追踪单）"""
    all_data = []
    for ord_type in ["conditional", "move_order_stop", "oco"]:
        try:
            algo = ex.private_get_trade_orders_algo_pending({
                "instType": "SWAP", "instId": inst_id, "ordType": ord_type
            })
            all_data.extend(algo.get("data", []))
        except Exception as e:
            print(f"  ⚠️ 查询 {ord_type} 单失败: {e}")
    return all_data


def cmd_status(args):
    ex = get_exchange()
    inst_id = args.symbol
    ccxt_sym = to_ccxt_symbol(inst_id)

    print(f"=== 状态 {inst_id} ===")
    positions = ex.private_get_account_positions({"instType": "SWAP", "instId": inst_id})
    pos = [p for p in positions.get("data", []) if float(p.get("pos", 0)) != 0]
    if pos:
        for p in pos:
            side = p.get("posSide") or ("long" if float(p["pos"]) > 0 else "short")
            print(f"  持仓: {'多' if side == 'long' else '空'} {abs(float(p['pos']))}张 | "
                  f"均价 {p.get('avgPx')} | 未实现 {p.get('upl')} | 保证金 {p.get('margin')} | "
                  f"杠杆 {p.get('lever')}x")
    else:
        print("  无持仓")

    algo = fetch_pending_algos(ex, inst_id)
    for a in algo:
        kind = "追踪" if a.get("ordType") == "move_order_stop" else ("止损" if a.get("slTriggerPx") else "止盈")
        print(f"  algo[{a['algoId']}]: {kind} | 触发 {a.get('slTriggerPx') or a.get('tpTriggerPx') or a.get('triggerPx')} | "
              f"{a.get('sz')}张 | {a.get('state')}")


def cmd_cleanup(args):
    ex = get_exchange()
    print(f"=== 清理 {args.symbol} 废单 ===")
    pending = fetch_pending_algos(ex, args.symbol)
    if not pending:
        print("  ✅ 无残留条件单")
        return
    to_cancel = [{"algoId": a["algoId"], "instId": args.symbol} for a in pending]
    r = ex.private_post_trade_cancel_algos(to_cancel)
    print(f"  🧹 已清理 {len(to_cancel)} 个: {json.dumps(r, ensure_ascii=False)}")


def main():
    ap = argparse.ArgumentParser(description="OKX 交易执行工具 (改动3/4/6)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_open = sub.add_parser("open")
    p_open.add_argument("--symbol", required=True, help="如 PEPE-USDT-SWAP")
    p_open.add_argument("--side", choices=["long", "short"], default="long")
    p_open.add_argument("--margin", type=float, default=5.0)
    p_open.add_argument("--mode", choices=["ladder", "fixed"], default="ladder")
    p_open.add_argument("--leverage", type=int, default=0, help="覆盖默认杠杆 (未验证币种默认强制5x)")
    p_open.add_argument("--skip-filters", action="store_true")
    p_open.add_argument("--skip-memory", action="store_true", help="跳过交易前记忆回查")

    p_close = sub.add_parser("close")
    p_close.add_argument("--symbol", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--symbol", required=True)

    p_cleanup = sub.add_parser("cleanup")
    p_cleanup.add_argument("--symbol", required=True)

    args = ap.parse_args()
    if args.cmd == "open":
        cmd_open(args)
    elif args.cmd == "close":
        cmd_close(args=args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "cleanup":
        cmd_cleanup(args)


if __name__ == "__main__":
    main()
