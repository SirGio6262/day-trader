#!/usr/bin/env python3
"""Replay the opening-range rule over past sessions, long and short, with the real limits.

    python autotrader/backtest.py fetch --days 90 --out bars.json     # Alpaca IEX 5-minute + daily bars
    python autotrader/backtest.py run bars.json [--config trader/config.json] [--json out.json]

Long: the first 5-minute bar completing inside the entry window that CLOSES above the range high; entry at
the next bar's open (a market order after the bar completes fills at the next print), stop at the range
midpoint, target at target_r times the risk. Short: the mirror image under the range low. Each side is
run as its own experiment with the config's max loss per trade, daily loss, trades per day and whole-share
sizing; "both" runs them together under the same daily limits. A bar that touches both the stop and the
target counts as a stop. Open at 15:55 is closed at that bar's open. Costs: the journal's
cost_per_share_round_trip. A short on a day the stock is already down 10 % from the previous close is
marked SSR (Reg SHO 201: a market short would not fill as planned) and not taken.

This is history, not a forecast. It says whether a rule deserves paper trades, nothing more."""

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autotrader as at  # noqa: E402

NY = at.NY


def fetch(days, out, symbols, feed):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("ALPACA_API_KEY / ALPACA_SECRET_KEY are not in the environment")
    client = StockHistoricalDataClient(key, secret)
    end = dt.datetime.now(tz=dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    data = {"fetched": end.isoformat(), "feed": feed, "symbols": symbols, "bars": {}, "daily": {}}
    for tf, slot in ((TimeFrame(5, TimeFrameUnit.Minute), "bars"), (TimeFrame.Day, "daily")):
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=tf, start=start, end=end, feed=DataFeed(feed))
        bs = client.get_stock_bars(req)
        for sym, bars in bs.data.items():
            data[slot][sym] = [[b.timestamp.astimezone(NY).isoformat(), float(b.open), float(b.high), float(b.low),
                                float(b.close), float(b.volume)] for b in bars]
        print("%s: %d symbols, %d bars" % (slot, len(bs.data), sum(len(v) for v in bs.data.values())))
    with open(out, "w") as fh:
        json.dump(data, fh)
    print("written", out)


def load(path):
    with open(path) as fh:
        data = json.load(fh)
    bars = {}
    for sym, rows in data["bars"].items():
        for ts, o, h, l, c, v in rows:
            t = dt.datetime.fromisoformat(ts).astimezone(NY)
            bars.setdefault(sym, {}).setdefault(t.date().isoformat(), []).append(
                {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
    daily = {}
    for sym, rows in data.get("daily", {}).items():
        for ts, o, h, l, c, v in rows:
            daily.setdefault(sym, {})[dt.datetime.fromisoformat(ts).astimezone(NY).date().isoformat()] = c
    return bars, daily


def prev_close(daily, sym, date):
    earlier = [d for d in daily.get(sym, {}) if d < date]
    return daily[sym][max(earlier)] if earlier else None


def signal(sb, orr, side, range_end, entry_from, entry_until):
    """The first bar completing inside the window that closes beyond the range, and the bar after it."""
    sb = sorted(sb, key=lambda b: b["t"])
    for i, b in enumerate(sb):
        if b["t"] < range_end:
            continue
        done = b["t"] + dt.timedelta(minutes=5)
        if done < entry_from:
            continue
        if done > entry_until:
            return None, None
        hit = b["c"] > orr["high"] if side == "long" else b["c"] < orr["low"]
        if hit:
            return b, (sb[i + 1] if i + 1 < len(sb) else None)
    return None, None


def walk(sb, entry_bar_t, entry, stop, target, side, flat_by):
    """Exit price, time and reason over the bars after the entry bar."""
    for b in sorted(sb, key=lambda b: b["t"]):
        if b["t"] < entry_bar_t:
            continue
        if b["t"] >= flat_by:
            return b["o"], b["t"], "flatten"
        if side == "long":
            if b["l"] <= stop:
                return stop, b["t"], "stop"
            if b["h"] >= target:
                return target, b["t"], "target"
        else:
            if b["h"] >= stop:
                return stop, b["t"], "stop"
            if b["l"] <= target:
                return target, b["t"], "target"
    last = max(sb, key=lambda b: b["t"])
    return last["c"], last["t"], "flatten"


def candidates(bars, daily, auto, cfg, side):
    """Every signal of one side, sized by the config, with its outcome; before the daily limits."""
    out = []
    for sym, days in bars.items():
        for date, sb in days.items():
            day = dt.date.fromisoformat(date)
            session_open = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
            if not any(b["t"] == session_open for b in sb) or len(sb) < 60:
                continue       # a half day or a hole in the data
            orr = at.opening_range(sb, auto["or_minutes"], session_open)
            if not orr:
                continue
            range_end = session_open + dt.timedelta(minutes=auto["or_minutes"])
            entry_from = dt.datetime.combine(day, dt.time.fromisoformat(auto["entry_from"]), tzinfo=NY)
            entry_until = dt.datetime.combine(day, dt.time.fromisoformat(auto["entry_until"]), tzinfo=NY)
            flat_by = dt.datetime.combine(day, dt.time.fromisoformat(cfg.get("flat_by", "15:55")), tzinfo=NY)
            bar, nxt = signal(sb, orr, side, range_end, entry_from, entry_until)
            if bar is None or nxt is None:
                continue
            entry = nxt["o"]
            stop = orr["mid"]
            risk = (entry - stop) if side == "long" else (stop - entry)
            rec = {"symbol": sym, "date": date, "side": side, "signal_at": at.hm(bar["t"]), "entry_at": at.hm(nxt["t"]),
                   "or_high": orr["high"], "or_low": orr["low"], "entry": round(entry, 2), "stop": round(stop, 2),
                   "strength": abs(bar["c"] - (orr["high"] if side == "long" else orr["low"])) / bar["c"]}
            pc = prev_close(daily, sym, date)
            rec["gap_pct"] = round((sb[0]["o"] - pc) / pc * 100, 3) if pc else None
            if risk <= 0:
                rec["skipped"] = "stop on the wrong side after the next bar's open"
                out.append(rec)
                continue
            if risk / entry * 100 < auto["min_stop_distance_pct"]:
                rec["skipped"] = "stop too tight"
                out.append(rec)
                continue
            if side == "short" and pc and min(b["l"] for b in sb if b["t"] <= bar["t"]) <= 0.9 * pc:
                rec["skipped"] = "SSR"
                out.append(rec)
                continue
            target = entry + auto["target_r"] * risk if side == "long" else entry - auto["target_r"] * risk
            # size_order is long-only (it wants the stop below the entry); a short with the same risk per share
            # and the same price gets the same whole-share size, so it is sized through the mirrored stop.
            qty, limit, _ = at.size_order(cfg, entry, entry - risk, float(cfg.get("budget", 0)))
            if qty <= 0:
                rec["skipped"] = "no size (%s)" % limit
                out.append(rec)
                continue
            exit_price, exit_t, why = walk(sb, nxt["t"], entry, stop, target, side, flat_by)
            per_share = (exit_price - entry) if side == "long" else (entry - exit_price)
            cost = float((cfg.get("journal") or {}).get("cost_per_share_round_trip", 0.02))
            rec.update({"target": round(target, 2), "qty": qty, "risk_usd": round(risk * qty, 2),
                        "exit": round(exit_price, 2), "exit_at": at.hm(exit_t), "exit_by": why,
                        "r": round(per_share / risk, 3), "pnl_usd": round(per_share * qty - cost * qty, 2),
                        "entry_t": nxt["t"], "exit_t": exit_t})
            out.append(rec)
    return out


def apply_daily_limits(trades, cfg):
    """The autotrader's book: at most max_trades_per_day, and no entry whose max loss plus the open risk plus
    the losses already realized would pass max_daily_loss_usd. Candidates in time order, then by strength."""
    taken = []
    by_day = {}
    for tr in trades:
        by_day.setdefault(tr["date"], []).append(tr)
    max_n = int(cfg.get("max_trades_per_day", 3))
    daily = float(cfg["max_daily_loss_usd"])
    for date, day in by_day.items():
        book = []
        for tr in sorted(day, key=lambda x: (x["entry_t"], -x["strength"])):
            if len(book) >= max_n:
                tr["skipped"] = "daily trade limit"
                continue
            if any(b["symbol"] == tr["symbol"] for b in book):
                tr["skipped"] = "symbol already traded today"
                continue
            open_risk = sum(b["risk_usd"] for b in book if b["exit_t"] > tr["entry_t"])
            realized = sum(b["pnl_usd"] for b in book if b["exit_t"] <= tr["entry_t"])
            if open_risk + tr["risk_usd"] + max(0.0, -realized) > daily + 1e-9:
                tr["skipped"] = "daily loss limit"
                continue
            book.append(tr)
        taken.extend(book)
    return sorted(taken, key=lambda x: x["entry_t"])


def stats(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    mean = statistics.fmean(rs)
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else None
    wins = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
    losses = [-t["pnl_usd"] for t in trades if t["pnl_usd"] < 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in trades:
        eq += t["pnl_usd"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    by_exit = {}
    for t in trades:
        by_exit.setdefault(t["exit_by"], []).append(t["r"])
    return {"n": n, "win_rate": round(len(wins) / n, 3), "mean_r": round(mean, 3), "sd_r": round(sd, 3),
            "se_r": round(se, 3) if se else None, "t": round(mean / se, 2) if se else None,
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses else None,
            "pnl_usd": round(sum(t["pnl_usd"] for t in trades), 2), "max_drawdown_usd": round(dd, 2),
            "sessions": len({t["date"] for t in trades}),
            "by_exit": {k: {"n": len(v), "mean_r": round(statistics.fmean(v), 3)} for k, v in sorted(by_exit.items())}}


def run(path, cfg_path, json_out):
    cfg = at.read_json(cfg_path) or {}
    auto = dict(at.DEFAULTS)
    auto.update(cfg.get("autotrader") or {})
    bars, daily = load(path)
    sessions = sorted({d for days in bars.values() for d in days})
    report = {"sessions": len(sessions), "from": sessions[0], "to": sessions[-1], "symbols": sorted(bars),
              "limits": {k: cfg.get(k) for k in ("max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day", "budget")},
              "sides": {}}
    pools = {}
    for side in ("long", "short"):
        cands = candidates(bars, daily, auto, cfg, side)
        pools[side] = [c for c in cands if "skipped" not in c]
        skipped = {}
        for c in cands:
            if "skipped" in c:
                skipped[c["skipped"]] = skipped.get(c["skipped"], 0) + 1
        taken = apply_daily_limits([dict(c) for c in pools[side]], cfg)
        report["sides"][side] = {"signals": len(cands), "skipped_before_limits": skipped,
                                 "all_signals": stats(pools[side]), "with_daily_limits": stats(taken),
                                 "trades": [{k: v for k, v in t.items() if k not in ("entry_t", "exit_t", "strength")}
                                            for t in taken]}
    both = apply_daily_limits([dict(c) for c in pools["long"] + pools["short"]], cfg)
    report["sides"]["both"] = {"with_daily_limits": stats(both),
                               "long_share": round(sum(1 for t in both if t["side"] == "long") / len(both), 3) if both else None}
    if json_out:
        with open(json_out, "w") as fh:
            json.dump(report, fh, indent=1, default=str)
    print("%d sessions %s to %s, %d symbols; limits %s" % (report["sessions"], report["from"], report["to"],
                                                           len(report["symbols"]), report["limits"]))
    for side, r in report["sides"].items():
        print("\n== %s ==" % side)
        if "signals" in r:
            print("signals %d, skipped before limits: %s" % (r["signals"], r["skipped_before_limits"]))
            print("all signals:        %s" % line(r["all_signals"]))
        print("with daily limits:  %s" % line(r["with_daily_limits"]))
        if "by_exit" in r["with_daily_limits"]:
            print("by exit:            %s" % r["with_daily_limits"]["by_exit"])


def line(s):
    if not s.get("n"):
        return "no trades"
    return ("n=%d win %.0f%% meanR %+.3f se %s t %s PF %s P&L $%.2f maxDD $%.2f over %d sessions"
            % (s["n"], s["win_rate"] * 100, s["mean_r"], s["se_r"], s["t"], s["profit_factor"], s["pnl_usd"],
               s["max_drawdown_usd"], s["sessions"]))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--days", type=int, default=90)
    f.add_argument("--out", default="bars.json")
    f.add_argument("--config", default="trader/config.json")
    r = sub.add_parser("run")
    r.add_argument("bars")
    r.add_argument("--config", default="trader/config.json")
    r.add_argument("--json")
    a = p.parse_args()
    cfg = at.read_json(a.config) or {}
    auto = dict(at.DEFAULTS)
    auto.update(cfg.get("autotrader") or {})
    if a.cmd == "fetch":
        fetch(a.days, a.out, auto["symbols"], auto["feed"])
    else:
        run(a.bars, a.config, a.json)


if __name__ == "__main__":
    main()
