#!/usr/bin/env python3
"""Trade journal and the go-live gate for trader-journal. Standard library only.

The other ten skills find, time, size and place one trade at a time. None of
them answers the only question that decides whether you keep any money: does
this setup, traded your way, have a positive expectancy after costs? This tool
does. It reads the fills trader-broker records, pairs each buy with its sell,
measures every closed trade in R (profit divided by the risk you took at the
stop) and in dollars, and prints the statistics a physicist would want before
betting: the sample size, the mean, its standard error, a t-statistic, a
profit factor and the drawdown. Then it gives one verdict against the gate in
your config: NOT YET, GO (small, live), or STOP.

  report    the statistics and the verdict, for paper or live trades.
  add       records a trade made outside the pack (another broker, by hand).
  list      every closed trade, one line each, oldest first.
  csv       writes every closed trade to a CSV for a spreadsheet.

    python3 journal.py report                       # paper trades (the default)
    python3 journal.py report --live                # live trades only
    python3 journal.py report --json
    python3 journal.py add --symbol TSLA --date 2026-10-19 --qty 8 \\
        --entry 364.18 --exit 358.32 --stop 358.32 --setup orb --note "stopped"
    python3 journal.py list
    python3 journal.py csv trader/journal.csv

Sources: trader/state/broker.json (fills recorded with ticket.py fill, plus
the stop of the order you approved) and trader/journal.json (trades added
here). Costs: the config's journal.cost_per_share_round_trip (default 0.02,
for the spread and slippage on a commission-free broker) is taken off every
trade, so every number here is net.
"""

import argparse
import csv
import datetime
import json
import math
import os
import sys

DEFAULT_GATE = {
    "min_trades": 60,           # closed trades before any verdict but NOT YET
    "min_expectancy_r": 0.10,   # mean R per trade, net of costs
    "min_t": 2.0,               # mean R / standard error of the mean
    "min_profit_factor": 1.3,   # gross wins / gross losses, in dollars
    "max_drawdown_pct": 10.0,   # worst peak-to-trough fall, % of the budget
    "stop_trades": 30,          # after this many, a clearly negative mean says STOP
    "stop_t": -2.0,
    "cost_per_share_round_trip": 0.02,
}


class JournalError(Exception):
    pass


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not os.path.exists(path):
        if required:
            raise JournalError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise JournalError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def gate_from(cfg):
    g = dict(DEFAULT_GATE)
    for k, v in (cfg.get("journal") or {}).items():
        if k in g and v is not None:
            g[k] = float(v) if k != "min_trades" and k != "stop_trades" else int(v)
    return g


def _f(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _money(x):
    return ("-$%s" if x < 0 else "$%s") % format(abs(x), ",.2f")


def _na(x):
    return "n/a" if x is None else x


# ---------------------------------------------------------------- trades

def trade(symbol, date, qty, entry, exit_price, stop, cost_rt, source, setup=None, note=None,
          paper=True, entry_at=None, exit_at=None):
    """One closed long trade, net of the round-trip cost per share."""
    qty, entry, exit_price = float(qty), float(entry), float(exit_price)
    gross = (exit_price - entry) * qty
    net = gross - cost_rt * qty
    risk_ps = (entry - float(stop)) if stop is not None else None
    r = None
    if risk_ps is not None and risk_ps > 0:
        r = (net / qty) / risk_ps
    return {"symbol": symbol.upper(), "date": date, "qty": qty, "entry": round(entry, 4),
            "exit": round(exit_price, 4), "stop": (round(float(stop), 4) if stop is not None else None),
            "risk_per_share": (round(risk_ps, 4) if risk_ps is not None else None),
            "pnl_gross": round(gross, 2), "pnl": round(net, 2), "r": (round(r, 3) if r is not None else None),
            "setup": setup or "", "note": note or "", "source": source, "paper": bool(paper),
            "entry_at": entry_at, "exit_at": exit_at}


def trades_from_broker_log(state, cost_rt, paper_only=None):
    """Pair buy fills with sell fills per symbol, oldest first (FIFO). The stop
    comes from the last approved or placed buy of that symbol on or before the
    buy fill. A sell that closes more than one buy is split across them."""
    recs = sorted(state.get("orders", []), key=lambda r: (str(r.get("at", "")), ))
    stops = {}      # symbol -> list of (at, stop, paper)
    for r in recs:
        if r.get("status") in ("approved", "placed") and str(r.get("side", "")).lower() == "buy" \
                and _f(r.get("stop")) is not None:
            stops.setdefault(str(r.get("symbol", "")).upper(), []).append(
                (str(r.get("at", "")), _f(r.get("stop")), r.get("paper", True)))
    open_lots = {}  # symbol -> list of dicts
    out = []
    for r in recs:
        if r.get("status") != "filled":
            continue
        sym = str(r.get("symbol", "")).upper()
        side = str(r.get("side", "")).lower()
        qty, price = _f(r.get("qty")), _f(r.get("price"))
        if not sym or qty is None or price is None or qty <= 0:
            continue
        at = str(r.get("at", ""))
        if side == "buy":
            stop, paper = None, True
            cands = [s for s in stops.get(sym, []) if s[0] <= at or s[0][:10] == at[:10]]
            if cands:
                stop, paper = cands[-1][1], cands[-1][2]
            open_lots.setdefault(sym, []).append({"qty": qty, "price": price, "stop": stop, "at": at,
                                                  "date": r.get("date") or at[:10], "paper": paper})
        elif side == "sell":
            left = qty
            lots = open_lots.get(sym, [])
            while left > 1e-9 and lots:
                lot = lots[0]
                take = min(left, lot["qty"])
                out.append(trade(sym, lot["date"], take, lot["price"], price, lot["stop"], cost_rt,
                                 "broker-log", paper=lot["paper"], entry_at=lot["at"], exit_at=at,
                                 setup="", note=""))
                lot["qty"] -= take
                left -= take
                if lot["qty"] <= 1e-9:
                    lots.pop(0)
    still_open = {s: round(sum(l["qty"] for l in lots), 6) for s, lots in open_lots.items() if lots}
    if paper_only is not None:
        out = [t for t in out if bool(t["paper"]) == bool(paper_only)]
    return out, still_open


def trades_from_journal(doc, cost_rt, paper_only=None):
    out = []
    for t in (doc or {}).get("trades", []):
        try:
            out.append(trade(t["symbol"], t["date"], t["qty"], t["entry"], t["exit"], t.get("stop"), cost_rt,
                             "journal", setup=t.get("setup"), note=t.get("note"), paper=t.get("paper", True)))
        except (KeyError, TypeError, ValueError):
            continue
    if paper_only is not None:
        out = [t for t in out if bool(t["paper"]) == bool(paper_only)]
    return out


def all_trades(a, cfg, paper_only):
    cost_rt = gate_from(cfg)["cost_per_share_round_trip"]
    state = read_json(a.state, "broker log", required=False) or {}
    log_trades, still_open = trades_from_broker_log(state, cost_rt, paper_only)
    manual = trades_from_journal(read_json(a.journal, "journal", required=False), cost_rt, paper_only)
    trades = sorted(log_trades + manual, key=lambda t: (t["date"], t.get("exit_at") or "", t["symbol"]))
    return trades, still_open, cost_rt


# ---------------------------------------------------------------- statistics

def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _sd(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def stats(trades, budget=None):
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    rs = [t["r"] for t in trades if t["r"] is not None]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    s = {"trades": n, "with_r": len(rs), "wins": len(wins), "losses": len(losses), "flat": n - len(wins) - len(losses),
         "win_rate": (len(wins) / n if n else None), "pnl_total": round(sum(pnl), 2),
         "pnl_mean": (round(_mean(pnl), 2) if n else None),
         "avg_win": (round(_mean(wins), 2) if wins else None), "avg_loss": (round(-_mean(losses), 2) if losses else None),
         "profit_factor": (round(gross_win / gross_loss, 3) if gross_loss > 0 else None),
         "expectancy_r": (round(_mean(rs), 3) if rs else None), "sd_r": None, "se_r": None, "t_r": None,
         "ci95_r": None, "avg_win_r": None, "avg_loss_r": None,
         "max_drawdown": 0.0, "max_drawdown_pct": None, "max_consecutive_losses": 0}
    if rs:
        wr = [r for r in rs if r > 0]
        lr = [r for r in rs if r < 0]
        s["avg_win_r"] = round(_mean(wr), 3) if wr else None
        s["avg_loss_r"] = round(-_mean(lr), 3) if lr else None
        sd = _sd(rs)
        if sd is not None:
            s["sd_r"] = round(sd, 3)
            se = sd / math.sqrt(len(rs))
            s["se_r"] = round(se, 4)
            if se > 0:
                s["t_r"] = round(_mean(rs) / se, 2)
                s["ci95_r"] = [round(_mean(rs) - 1.96 * se, 3), round(_mean(rs) + 1.96 * se, 3)]
    peak = cum = 0.0
    dd = 0.0
    streak = worst = 0
    for p in pnl:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        streak = streak + 1 if p < 0 else 0
        worst = max(worst, streak)
    s["max_drawdown"] = round(dd, 2)
    s["max_consecutive_losses"] = worst
    if budget:
        s["max_drawdown_pct"] = round(100.0 * dd / float(budget), 2)
    return s


def by_key(trades, key):
    groups = {}
    for t in trades:
        groups.setdefault(t.get(key) or "(none)", []).append(t)
    return {k: stats(v) for k, v in sorted(groups.items())}


def verdict(s, gate, budget=None):
    """One verdict and the reasons. The order matters: a clear loser is told to
    stop before the sample is full; a full sample must clear every bar."""
    n = s["trades"]
    reasons = []
    if n >= gate["stop_trades"] and s["t_r"] is not None and s["t_r"] <= gate["stop_t"]:
        return "STOP", ["%d trades and the mean R is %s with t = %s: this setup, traded this way, loses. "
                        "Change the rule or the market before you trade it again, paper or live."
                        % (n, s["expectancy_r"], s["t_r"])]
    if n < gate["min_trades"]:
        reasons.append("%d closed trade%s; the gate needs %d before any verdict" % (n, "" if n == 1 else "s", gate["min_trades"]))
        if s["expectancy_r"] is not None:
            reasons.append("so far: mean R %s, t = %s, profit factor %s"
                           % (s["expectancy_r"], _na(s["t_r"]), _na(s["profit_factor"])))
        return "NOT YET", reasons
    ok = True
    if s["with_r"] < n:
        reasons.append("%d trade%s have no stop recorded, so no R: fix the log or add them with --stop"
                       % (n - s["with_r"], "" if n - s["with_r"] == 1 else "s"))
    if s["expectancy_r"] is None or s["expectancy_r"] < gate["min_expectancy_r"]:
        ok = False
        reasons.append("mean R %s is under %s" % (s["expectancy_r"], gate["min_expectancy_r"]))
    else:
        reasons.append("mean R %s clears %s" % (s["expectancy_r"], gate["min_expectancy_r"]))
    if s["t_r"] is None or s["t_r"] < gate["min_t"]:
        ok = False
        reasons.append("t = %s is under %s: the edge is not distinguishable from zero yet" % (s["t_r"], gate["min_t"]))
    else:
        reasons.append("t = %s clears %s" % (s["t_r"], gate["min_t"]))
    pf = s["profit_factor"]
    if pf is None or pf < gate["min_profit_factor"]:
        ok = False
        reasons.append("profit factor %s is under %s" % (pf, gate["min_profit_factor"]))
    else:
        reasons.append("profit factor %s clears %s" % (pf, gate["min_profit_factor"]))
    if budget:
        if s["max_drawdown_pct"] is not None and s["max_drawdown_pct"] > gate["max_drawdown_pct"]:
            ok = False
            reasons.append("max drawdown %s%% of the budget is over %s%%" % (s["max_drawdown_pct"], gate["max_drawdown_pct"]))
        else:
            reasons.append("max drawdown %s%% of the budget is within %s%%" % (s["max_drawdown_pct"], gate["max_drawdown_pct"]))
    else:
        reasons.append("no budget in the config, so the drawdown bar was not checked")
    return ("GO" if ok else "NOT YET"), reasons


# ---------------------------------------------------------------- commands

def _paper_flag(a):
    if getattr(a, "live", False):
        return False
    if getattr(a, "all", False):
        return None
    return True


def cmd_report(a):
    cfg = read_json(a.config, "config", required=False) or {}
    gate = gate_from(cfg)
    paper_only = _paper_flag(a)
    trades, still_open, cost_rt = all_trades(a, cfg, paper_only)
    budget = _f(cfg.get("budget"))
    s = stats(trades, budget)
    v, reasons = verdict(s, gate, budget)
    label = {True: "paper", False: "live", None: "paper and live"}[paper_only]
    if a.json:
        print(json.dumps({"scope": label, "cost_per_share_round_trip": cost_rt, "gate": gate, "stats": s,
                          "by_symbol": by_key(trades, "symbol"), "by_setup": by_key(trades, "setup"),
                          "still_open": still_open, "verdict": v, "reasons": reasons}, indent=2))
        return 0
    print("trader-journal: %d closed %s trade%s, net of %s a share round trip"
          % (s["trades"], label, "" if s["trades"] == 1 else "s", _money(cost_rt).replace("$", "$")))
    if s["trades"]:
        print("  P&L %s total, %s a trade; %d won, %d lost, %d flat (win rate %.0f%%)"
              % (_money(s["pnl_total"]), _money(s["pnl_mean"]), s["wins"], s["losses"], s["flat"], 100 * s["win_rate"]))
        if s["avg_win"] is not None or s["avg_loss"] is not None:
            print("  average win %s, average loss %s, profit factor %s"
                  % (_money(s["avg_win"]) if s["avg_win"] is not None else "n/a",
                     _money(s["avg_loss"]) if s["avg_loss"] is not None else "n/a",
                     _na(s["profit_factor"]) if s["losses"] else "no losses yet"))
        if s["expectancy_r"] is not None:
            ci = s["ci95_r"]
            line = "  expectancy %s R a trade (%d trade%s with a stop)" % (
                s["expectancy_r"], s["with_r"], "" if s["with_r"] == 1 else "s")
            if s["sd_r"] is not None:
                line += "; sd %s, standard error %s, t = %s" % (s["sd_r"], s["se_r"], s["t_r"])
                if ci:
                    line += ", 95%% CI [%s, %s]" % (ci[0], ci[1])
            print(line)
            print("  average win %s R, average loss %s R" % (_na(s["avg_win_r"]), _na(s["avg_loss_r"])))
        dd = "  max drawdown %s" % _money(s["max_drawdown"])
        if s["max_drawdown_pct"] is not None:
            dd += " (%s%% of the %s budget)" % (s["max_drawdown_pct"], _money(budget))
        print(dd + ", worst losing streak %d" % s["max_consecutive_losses"])
        bs = by_key(trades, "symbol")
        if len(bs) > 1:
            print("  by symbol: " + "; ".join("%s %d trades, %s R" % (k, v["trades"], v["expectancy_r"]) for k, v in bs.items()))
    if still_open:
        print("  still open (not counted): " + ", ".join("%s x%s" % (k, v) for k, v in still_open.items()))
    print("Verdict: %s" % v)
    for r in reasons:
        print("  - " + r)
    if v == "GO":
        print("  GO means: live, at the SAME dollar risk per trade as the paper run, for the next %d trades, then report "
              "--live and compare. Not more size, not more trades a day." % gate["min_trades"])
    return 0


def cmd_add(a):
    doc = read_json(a.journal, "journal", required=False) or {"trades": []}
    if _f(a.qty) is None or _f(a.qty) <= 0:
        raise JournalError("--qty wants a positive number of shares")
    for k in ("entry", "exit"):
        if _f(getattr(a, k)) is None or _f(getattr(a, k)) <= 0:
            raise JournalError("--%s wants a positive price" % k)
    try:
        datetime.date.fromisoformat(a.date)
    except ValueError:
        raise JournalError("--date wants YYYY-MM-DD")
    stop = _f(a.stop)
    if stop is not None and stop >= _f(a.entry):
        raise JournalError("--stop must be below --entry for a long trade")
    rec = {"symbol": a.symbol.upper(), "date": a.date, "qty": _f(a.qty), "entry": _f(a.entry), "exit": _f(a.exit),
           "stop": stop, "setup": a.setup or "", "note": a.note or "", "paper": not a.live,
           "added_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    doc.setdefault("trades", []).append(rec)
    write_json(a.journal, doc)
    cfg = read_json(a.config, "config", required=False) or {}
    t = trade(rec["symbol"], rec["date"], rec["qty"], rec["entry"], rec["exit"], rec["stop"],
              gate_from(cfg)["cost_per_share_round_trip"], "journal", rec["setup"], rec["note"], rec["paper"])
    print("Recorded %s %s %s %s -> %s%s: %s net%s. %d trade%s in %s."
          % ("paper" if rec["paper"] else "LIVE", a.date, rec["symbol"], rec["entry"], rec["exit"],
             (" (stop %s)" % rec["stop"]) if stop is not None else " (no stop: no R)", _money(t["pnl"]),
             (", %s R" % t["r"]) if t["r"] is not None else "", len(doc["trades"]),
             "" if len(doc["trades"]) == 1 else "s", a.journal))
    return 0


def cmd_list(a):
    cfg = read_json(a.config, "config", required=False) or {}
    trades, still_open, _ = all_trades(a, cfg, _paper_flag(a))
    if not trades:
        print("No closed trades yet.")
        return 1
    cum = 0.0
    for t in trades:
        cum += t["pnl"]
        print("%s %-6s %6s x %-9s -> %-9s stop %-9s %9s %7s  cum %10s  %s%s"
              % (t["date"], t["symbol"], ("%g" % t["qty"]), t["entry"], t["exit"], t["stop"] if t["stop"] is not None else "-",
                 _money(t["pnl"]), ("%+.2fR" % t["r"]) if t["r"] is not None else "   -  ", _money(cum),
                 "paper" if t["paper"] else "LIVE", (" " + t["note"]) if t["note"] else ""))
    if still_open:
        print("still open: " + ", ".join("%s x%s" % (k, v) for k, v in still_open.items()))
    return 0


def cmd_csv(a):
    cfg = read_json(a.config, "config", required=False) or {}
    trades, _, _ = all_trades(a, cfg, _paper_flag(a))
    cols = ["date", "symbol", "qty", "entry", "exit", "stop", "risk_per_share", "pnl_gross", "pnl", "r", "setup",
            "paper", "source", "entry_at", "exit_at", "note"]
    d = os.path.dirname(a.path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(a.path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)
    print("Wrote %d trade%s to %s." % (len(trades), "" if len(trades) == 1 else "s", a.path))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trade journal and the go-live gate (trader-journal).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--state", default="trader/state/broker.json", help="trader-broker's log")
        p.add_argument("--journal", default="trader/journal.json", help="trades added with `add`")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--live", action="store_true", help="live trades only (default: paper only)")
        g.add_argument("--all", action="store_true", help="paper and live together")

    p = sub.add_parser("report", help="statistics and the verdict")
    common(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("add", help="record a trade made outside the pack")
    common(p)
    for k in ("symbol", "date", "qty", "entry", "exit"):
        p.add_argument("--" + k, required=True)
    p.add_argument("--stop", help="the stop you had; without it the trade has no R")
    p.add_argument("--setup", help="orb, flag, level...")
    p.add_argument("--note")
    p = sub.add_parser("list", help="every closed trade")
    common(p)
    p = sub.add_parser("csv", help="write the trades to a CSV")
    common(p)
    p.add_argument("path")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"report": cmd_report, "add": cmd_add, "list": cmd_list, "csv": cmd_csv}[a.cmd](a)
    except JournalError as e:
        print("trader-journal: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
