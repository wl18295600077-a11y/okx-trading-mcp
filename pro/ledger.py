"""
交易账本 ledger.py
==================
SQLite 账本: 记录每笔开/平仓、盈亏、手续费、信号快照。

- execute_trade.py 的 open/close 自动写入本账本
- 独立 CLI:
    python ledger.py report [--last 10]   # 交易报表 + 汇总(胜率/累计盈亏/手续费)
    python ledger.py status               # 当前在仓列表
    python ledger.py reconcile            # 对账: 补录服务端自动平掉的仓(TP/SL/追踪触发)

数据文件: ledger.db (与脚本同目录; 可用环境变量 LEDGER_DB 覆盖, 供测试)
"""
import argparse
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB = os.environ.get("LEDGER_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ledger.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open TEXT NOT NULL,
    ts_close TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    mode TEXT NOT NULL,
    sz REAL NOT NULL,
    ct_val REAL NOT NULL DEFAULT 1,
    entry_px REAL NOT NULL,
    exit_px REAL,
    margin REAL,
    leverage REAL,
    sl_px REAL,
    tp_px REAL,
    trail_pct REAL,
    max_hold_h REAL,
    fee_open REAL NOT NULL DEFAULT 0,
    fee_close REAL NOT NULL DEFAULT 0,
    pnl REAL,
    pnl_pct REAL,
    close_reason TEXT,
    filters TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_open(symbol, side, mode, sz, ct_val, entry_px, margin, leverage,
                sl_px=None, tp_px=None, trail_pct=None, max_hold_h=None,
                fee=0.0, filters=None, note=None):
    """记录一笔开仓, 返回 trade id"""
    conn = get_conn()
    conn.execute(
        """INSERT INTO trades
           (ts_open, symbol, side, mode, sz, ct_val, entry_px, margin, leverage,
            sl_px, tp_px, trail_pct, max_hold_h, fee_open, filters, status, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
        (now_iso(), symbol, side, mode, sz, ct_val, entry_px, margin, leverage,
         sl_px, tp_px, trail_pct, max_hold_h, fee,
         json.dumps(filters, ensure_ascii=False, default=float) if filters else None, note))
    conn.commit()
    tid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.close()
    print(f"  📒 账本: 开仓 #{tid} {symbol} {side} {sz}张 @ {entry_px}")
    return tid


def record_close(symbol, exit_px, fee=0.0, close_reason="manual", ct_val=None, note=None):
    """平仓记账: 按 symbol 匹配最新 open 记录, 计算盈亏(含手续费)"""
    conn = get_conn()
    t = conn.execute(
        "SELECT * FROM trades WHERE symbol=? AND status='open' ORDER BY id DESC LIMIT 1",
        (symbol,)).fetchone()
    if t is None:
        conn.close()
        print(f"  ⚠️ 账本: 找不到 {symbol} 的 open 记录, 跳过 (可运行 reconcile 补录)")
        return None
    if exit_px is None:
        print(f"  ⚠️ 账本: 平仓价缺失, 跳过 (可运行 reconcile 补录)")
        return None
    ct_val = ct_val or t["ct_val"]
    direction = 1 if t["side"] == "long" else -1
    pnl = (exit_px - t["entry_px"]) * t["sz"] * ct_val * direction - (t["fee_open"] + fee)
    pnl_pct = (pnl / t["margin"] * 100) if t["margin"] else None
    conn.execute(
        """UPDATE trades SET ts_close=?, exit_px=?, fee_close=?, pnl=?, pnl_pct=?,
           close_reason=?, status='closed', note=? WHERE id=?""",
        (now_iso(), exit_px, fee, pnl, pnl_pct, close_reason, note, t["id"]))
    conn.commit()
    conn.close()
    print(f"  📒 账本: 平仓 #{t['id']} {symbol} | 盈亏 {pnl:+.4f} USDT "
          f"({pnl_pct:+.2f}%按保证金) | 原因 {close_reason}")
    return t["id"]


def make_exchange():
    """轻量 ccxt OKX 连接 (env 加载逻辑与 execute_trade.py 一致)"""
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
    ex = ccxt.okx({
        "apiKey": os.environ.get("OKX_API_KEY") or os.environ.get("OKX_APIKEY"),
        "secret": os.environ.get("OKX_SECRET") or os.environ.get("OKX_API_SECRET"),
        "password": os.environ.get("OKX_PASSPHRASE"),
        "enableRateLimit": True,
    })
    ex.load_markets()
    return ex


def reconcile(ex):
    """对账: 账本 open 记录 vs OKX 实际持仓。
    服务端已自动平掉的仓(TP/SL/追踪触发, 不经 cmd_close) → 从成交历史补录。"""
    conn = get_conn()
    open_trades = conn.execute("SELECT * FROM trades WHERE status='open' ORDER BY id").fetchall()
    conn.close()
    if not open_trades:
        print("  ✅ 账本无 open 记录")
        return
    for t in open_trades:
        symbol = t["symbol"]
        try:
            pos = ex.private_get_account_positions({"instType": "SWAP", "instId": symbol})
        except Exception as e:
            print(f"  ⚠️ #{t['id']} {symbol} 持仓查询失败: {e}")
            continue
        live = [p for p in pos.get("data", []) if float(p.get("pos", 0)) != 0
                and (not p.get("posSide") or p.get("posSide") == t["side"])]
        if live:
            print(f"  ⏳ #{t['id']} {symbol}: OKX 仍持{t['side']}仓, 跳过")
            continue
        # 服务端已平 → 查成交历史补录
        try:
            fills = ex.private_get_trade_fills_history(
                {"instType": "SWAP", "instId": symbol}).get("data", [])
        except Exception as e:
            print(f"  ⚠️ #{t['id']} {symbol} 成交查询失败: {e}")
            continue
        close_side = "sell" if t["side"] == "long" else "buy"
        # 账本 ts_open 是 UTC ISO, fills 的 ts 是毫秒时间戳 → 统一转毫秒比较
        ts_open_ms = int(datetime.strptime(
            t["ts_open"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
        sel = [f for f in fills if f.get("side") == close_side
               and f.get("posSide") == t["side"]
               and int(f.get("ts", 0)) >= ts_open_ms]
        if not sel:
            print(f"  ⚠️ #{t['id']} {symbol}: OKX 无持仓但找不到平仓成交, 需人工核查")
            continue
        tot_amt = sum(abs(float(f.get("fillPx", 0))) * float(f.get("fillSz", 0)) for f in sel)
        tot_sz = sum(float(f.get("fillSz", 0)) for f in sel)
        exit_px = tot_amt / tot_sz if tot_sz else t["entry_px"]
        fee = sum(abs(float(f.get("fee", 0))) for f in sel)
        # 推断平仓原因: 出场价贴近 SL/TP 触发价
        reason = "auto"
        if t["sl_px"] and abs(exit_px - t["sl_px"]) / t["sl_px"] < 0.01:
            reason = "SL"
        elif t["tp_px"] and abs(exit_px - t["tp_px"]) / t["tp_px"] < 0.01:
            reason = "TP"
        record_close(symbol, exit_px, fee, reason)


def report(last=None):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
    conn.close()
    if not rows:
        print("  📭 账本为空 — 还没有交易记录")
        return
    rows = rows[-last:] if last else rows
    hdr = (f"{'#':>3} {'symbol':<16} {'side':<5} {'mode':<6} "
           f"{'开仓':<17} {'平仓':<17} {'张':>3} {'入场':>11} {'出场':>11} "
           f"{'盈亏USDT':>10} {'收益率':>8} {'原因':<6}")
    print(hdr)
    print("-" * len(hdr))
    for t in rows:
        print(f"{t['id']:>3} {t['symbol']:<16} {t['side']:<5} {t['mode']:<6} "
              f"{t['ts_open']:<17} {(t['ts_close'] or '—'):<17} {t['sz']:>4.4g} "
              f"{t['entry_px']:>11.6g} {(t['exit_px'] or 0):>11.6g} "
              f"{(t['pnl'] or 0):>+10.4f} {(t['pnl_pct'] or 0):>+7.2f}% "
              f"{(t['close_reason'] or '在仓'):<6}")
    closed = [t for t in rows if t["status"] == "closed"]
    if closed:
        wins = [t for t in closed if (t["pnl"] or 0) > 0]
        tot_pnl = sum(t["pnl"] or 0 for t in closed)
        tot_fee = sum((t["fee_open"] or 0) + (t["fee_close"] or 0) for t in closed)
        print("-" * len(hdr))
        print(f"  汇总: 已平 {len(closed)} 笔 | 胜率 {len(wins)/len(closed)*100:.0f}% | "
              f"累计盈亏 {tot_pnl:+.4f} USDT | 手续费 {tot_fee:.4f} | 在仓 {len(rows)-len(closed)} 笔")


def status():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades WHERE status='open' ORDER BY id").fetchall()
    conn.close()
    if not rows:
        print("  📭 无在仓记录")
        return
    print(f"{'#':>3} {'symbol':<16} {'side':<5} {'开仓':<17} {'张':>3} {'入场':>11} "
          f"{'SL':>11} {'TP':>11} {'保证金':>7}")
    for t in rows:
        print(f"{t['id']:>3} {t['symbol']:<16} {t['side']:<5} {t['ts_open']:<17} "
              f"{t['sz']:>4.4g} {t['entry_px']:>11.6g} {(t['sl_px'] or 0):>11.6g} "
              f"{(t['tp_px'] or 0):>11.6g} {t['margin'] or 0:>7.2f}")


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "cp65001"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="交易账本 (SQLite)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_r = sub.add_parser("report")
    p_r.add_argument("--last", type=int, default=None, help="只看最近 N 笔")
    sub.add_parser("status")
    sub.add_parser("reconcile")
    args = ap.parse_args()
    if args.cmd == "report":
        report(args.last)
    elif args.cmd == "status":
        status()
    elif args.cmd == "reconcile":
        reconcile(make_exchange())


if __name__ == "__main__":
    main()
