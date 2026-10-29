#!/usr/bin/env python3
"""autotrader: the unattended executor for the day-trader project.

It trades ONE mechanical rule, the opening-range breakout from trader-scanner
and trader-entry, on a fixed list of liquid symbols, through Alpaca, and
writes every decision and fill into trader/state/broker.json in the same
shape trader-broker uses, so /trader-journal reads it unchanged. The rule:

  1. The opening range is the high and low of the first `or_minutes` (15) of
     the session, 9:30 to 9:45 New York, from 5-minute bars.
  2. The entry is the first COMPLETED 5-minute bar that closes above the
     range high, between `entry_from` (09:50) and `entry_until` (10:30).
  3. The stop is the range midpoint, rounded down to the cent. The target is
     `target_r` (2.0) times the risk above the entry. Both go to the broker
     inside ONE bracket order, so they hold even if this process dies.
  4. The size is the whole number of shares that fits every limit in
     trader/config.json: max loss per trade, position cap, max order, cash
     after the buffer. The risk check applies the rest: the daily loss limit,
     trades per day, the entry window, the blocked list, reward to risk.
  5. Anything still open at `flat_by` (15:55) is closed at market.
  6. A file named trader/STOP stops new entries and flattens at once.

It never shorts, never trades options or crypto, never adds to a position,
never moves a stop. Paper unless trader/config.json AND the environment both
say live. Commands:

    autotrader.py check            config, keys, account, clock, one bar request
    autotrader.py day              run today's session, then exit
    autotrader.py daemon           sleep until each session, run it, repeat
    autotrader.py flatten          close everything now, cancel open orders
    autotrader.py status           positions, open orders, today's log

Environment: ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER_TRADE (true),
optional TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for phone notifications.
Needs alpaca-py (pip install alpaca-py); the rule itself is plain Python.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8
    ZoneInfo = None

NY = ZoneInfo("America/New_York") if ZoneInfo else None

DEFAULTS = {
    "symbols": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO", "NFLX",
                "JPM", "BAC", "XOM", "COST", "WMT", "HD", "LLY", "V", "MA", "CRM"],
    "or_minutes": 15,
    "entry_from": "09:50",
    "entry_until": "10:30",
    "target_r": 2.0,
    "min_bar_volume": 0,
    "min_stop_distance_pct": 0.15,   # a stop closer than this to the entry cannot survive the spread
    "poll_seconds": 15,
    "feed": "iex",
    "max_symbols": 30,
}


DONE = ("filled", "canceled", "expired", "rejected")   # order states that hold no shares
NOT_OURS = ("manual", "carried")                        # positions without a bracket of this process
RELEASE_WAIT_SECONDS = 30


class AutoError(Exception):
    pass


# ---------------------------------------------------------------- time

def now_ny():
    return dt.datetime.now(NY)


def hm(t):
    return t.strftime("%H:%M")


def at_time(day, s):
    """A New York datetime for HH:MM on the given date."""
    h, m = (int(x) for x in s.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=NY)


def ny_iso(t):
    return t.astimezone(NY).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- files

def read_json(path, required=False):
    if not os.path.exists(path):
        if required:
            raise AutoError("missing %s" % path)
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
        fh.write("\n")
    os.replace(tmp, path)


class Paths:
    def __init__(self, root="trader"):
        self.root = root
        self.config = os.path.join(root, "config.json")
        self.broker_log = os.path.join(root, "state", "broker.json")
        self.state = os.path.join(root, "state", "autotrader.json")
        self.log = os.path.join(root, "state", "autotrader.log")
        self.stop = os.path.join(root, "STOP")
        self.watchlist = os.path.join(root, "watchlist.json")


def load_config(paths):
    cfg = read_json(paths.config, required=True)
    auto = dict(DEFAULTS)
    auto.update(cfg.get("autotrader") or {})
    if auto.get("use_scanner_watchlist"):
        wl = read_json(paths.watchlist) or {}
        syms = [str(x.get("ticker") or x.get("symbol") or x).upper() for x in wl.get("hits", wl.get("symbols", []))]
        if syms:
            auto["symbols"] = syms
    auto["symbols"] = [s.upper() for s in auto["symbols"]][: int(auto["max_symbols"])]
    for k in ("allow_short", "allow_options", "allow_margin", "allow_crypto"):
        if cfg.get(k):
            raise AutoError("%s is true in the config; the autotrader refuses to run with it" % k)
    if not cfg.get("require_stop", True):
        raise AutoError("require_stop is false; the autotrader needs a stop on every trade")
    if not cfg.get("max_loss_per_trade_usd") or not cfg.get("max_daily_loss_usd"):
        raise AutoError("max_loss_per_trade_usd and max_daily_loss_usd must be set")
    return cfg, auto


def paper_mode(cfg):
    """Paper unless the config AND the environment both say live."""
    env = os.environ.get("ALPACA_PAPER_TRADE", "true").strip().lower() not in ("false", "0", "no")
    cfg_paper = bool(cfg.get("paper", True))
    if env != cfg_paper:
        raise AutoError("paper mismatch: config says %s, ALPACA_PAPER_TRADE says %s; make them agree on purpose"
                        % ("paper" if cfg_paper else "LIVE", "paper" if env else "LIVE"))
    return cfg_paper


# ---------------------------------------------------------------- the rule (pure)

def opening_range(bars, or_minutes, session_open):
    """High, low and midpoint of the bars that START before open + or_minutes, or None if not all there."""
    end = session_open + dt.timedelta(minutes=or_minutes)
    inside = [b for b in bars if session_open <= b["t"] < end]
    need = or_minutes // 5
    if len(inside) < need:
        return None
    hi = max(b["h"] for b in inside)
    lo = min(b["l"] for b in inside)
    mid = math.floor((hi + lo) / 2 * 100) / 100.0
    return {"high": hi, "low": lo, "mid": mid, "bars": len(inside), "volume": sum(b["v"] for b in inside)}


def breakout_bar(bars, orr, now, session_open, or_minutes, entry_from, entry_until, min_volume=0):
    """The first completed 5-minute bar after the range that CLOSES above its high, inside the window."""
    range_end = session_open + dt.timedelta(minutes=or_minutes)
    for b in sorted(bars, key=lambda x: x["t"]):
        if b["t"] < range_end:
            continue
        done = b["t"] + dt.timedelta(minutes=5)
        if done > now:
            break
        if done < entry_from or done > entry_until:
            if done > entry_until:
                break
            continue
        if b["c"] > orr["high"] and b["v"] >= min_volume:
            return b
    return None


def plan_for(bar, orr, target_r, min_stop_pct):
    entry = float(bar["c"])
    stop = orr["mid"]
    if stop >= entry:
        return None, "stop %.2f is not below the entry %.2f" % (stop, entry)
    risk = entry - stop
    if risk / entry * 100 < min_stop_pct:
        return None, "stop %.2f is %.2f%% from the entry; too tight to survive the spread" % (stop, risk / entry * 100)
    target = math.floor((entry + target_r * risk) * 100) / 100.0
    return {"entry": round(entry, 2), "stop": round(stop, 2), "target": target, "risk_per_share": round(risk, 2),
            "reward_risk": round((target - entry) / risk, 2), "bar_time": hm(bar["t"])}, None


def size_order(cfg, entry, stop, cash):
    """Whole shares inside every limit; (qty, the limit that decided, lines)."""
    risk = entry - stop
    if risk <= 0:
        return 0, "stop not below entry", []
    limits = []
    limits.append(("max loss per trade", math.floor(float(cfg["max_loss_per_trade_usd"]) / risk)))
    budget = float(cfg.get("budget") or cash)
    if cfg.get("max_position_pct"):
        limits.append(("position cap", math.floor(budget * float(cfg["max_position_pct"]) / 100 / entry)))
    if cfg.get("max_order_usd"):
        limits.append(("max order", math.floor(float(cfg["max_order_usd"]) / entry)))
    buffer = budget * float(cfg.get("cash_buffer_pct", 0)) / 100
    limits.append(("cash after the buffer", math.floor(max(0.0, min(cash, budget) - buffer) / entry)))
    name, qty = min(limits, key=lambda x: x[1])
    qty = max(0, int(qty))
    if qty * entry < float(cfg.get("min_order_usd", 0)):
        return 0, "under the minimum order", limits
    return qty, name, limits


def today_entries(log, date):
    return [r for r in log.get("orders", []) if r.get("status") == "placed" and str(r.get("side", "")).lower() == "buy"
            and (r.get("date") or str(r.get("at", ""))[:10]) == date]


def realized_today(log, date):
    """Dollar P&L of today's closed round trips in the log (FIFO per symbol)."""
    lots, pnl = {}, 0.0
    for r in sorted(log.get("orders", []), key=lambda x: str(x.get("at", ""))):
        if r.get("status") != "filled" or (r.get("date") or str(r.get("at", ""))[:10]) != date:
            continue
        s, q, p = str(r.get("symbol", "")).upper(), float(r.get("qty", 0)), float(r.get("price", 0))
        if str(r.get("side", "")).lower() == "buy":
            lots.setdefault(s, []).append([q, p])
        else:
            left = q
            while left > 1e-9 and lots.get(s):
                lot = lots[s][0]
                take = min(left, lot[0])
                pnl += (p - lot[1]) * take
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-9:
                    lots[s].pop(0)
    return round(pnl, 2)


def risk_check(cfg, log, date, now, plan, qty, symbol, stop_file_present, open_risk):
    """Mirror of trader-risk for the one order the autotrader makes."""
    reasons = []
    if stop_file_present:
        reasons.append("trader/STOP exists: no new entries")
    if symbol in [str(b).upper() for b in cfg.get("blocked", [])]:
        reasons.append("%s is on the blocked list" % symbol)
    if qty <= 0:
        reasons.append("no size fits the limits")
    max_loss = round(qty * plan["risk_per_share"], 2)
    if max_loss > float(cfg["max_loss_per_trade_usd"]) + 1e-9:
        reasons.append("max loss %.2f is over the per-trade limit %.2f" % (max_loss, float(cfg["max_loss_per_trade_usd"])))
    if plan["reward_risk"] < float(cfg.get("min_reward_risk", 1.0)):
        reasons.append("reward:risk %.2f is under %.2f" % (plan["reward_risk"], float(cfg.get("min_reward_risk", 1.0))))
    entries = today_entries(log, date)
    if len(entries) >= int(cfg.get("max_trades_per_day", 3)):
        reasons.append("%d trades today already; the limit is %d" % (len(entries), int(cfg.get("max_trades_per_day", 3))))
    if any(str(e.get("symbol", "")).upper() == symbol for e in entries):
        reasons.append("%s was already traded today" % symbol)
    realized = realized_today(log, date)
    exposure = open_risk + max_loss + max(0.0, -realized)
    if exposure > float(cfg["max_daily_loss_usd"]) + 1e-9:
        reasons.append("open risk %.2f + this %.2f + realized losses %.2f would pass the daily limit %.2f"
                       % (open_risk, max_loss, max(0.0, -realized), float(cfg["max_daily_loss_usd"])))
    t = hm(now)
    if t < cfg.get("no_entries_before", "09:45") or t > cfg.get("no_entries_after", "15:30"):
        reasons.append("%s is outside the entry window %s to %s" % (t, cfg.get("no_entries_before", "09:45"),
                                                                    cfg.get("no_entries_after", "15:30")))
    return (len(reasons) == 0), reasons, max_loss


# ---------------------------------------------------------------- the broker log (trader-broker's shape)

def append_log(paths, rec):
    log = read_json(paths.broker_log) or {"orders": []}
    log.setdefault("orders", []).append(rec)
    write_json(paths.broker_log, log)


def log_placed(paths, now, date, symbol, qty, plan, max_loss, broker_id, paper, client_id):
    plain = ("BUY %d %s, market, good for today / stop-loss %.2f / take-profit %.2f / max loss about $%.2f "
             "if the stop fills at %.2f" % (qty, symbol, plan["stop"], plan["target"], max_loss, plan["stop"]))
    for status in ("approved", "placed"):
        rec = {"at": ny_iso(now), "date": date, "symbol": symbol, "side": "buy", "qty": str(qty), "kind": "entry",
               "max_loss": max_loss, "stop": plan["stop"], "target": plan["target"], "plain": plain, "status": status,
               "paper": paper, "broker": "alpaca", "source": "autotrader", "rule": "orb",
               "client_order_id": client_id}
        if status == "approved":
            rec["reply"] = "autotrader: the rule fired (%s bar closed %.2f above the %d-minute range high %.2f)" % (
                plan["bar_time"], plan["entry"], plan.get("or_minutes", 15), plan["or_high"])
        else:
            rec["broker_order_id"] = broker_id
        append_log(paths, rec)


def log_fill(paths, now, date, symbol, side, qty, price, note=None):
    rec = {"at": ny_iso(now), "date": date, "status": "filled", "symbol": symbol, "side": side, "qty": str(qty),
           "price": float(price), "plain": "%s %s %s at %.2f" % (side.upper(), qty, symbol, float(price)),
           "source": "autotrader"}
    if note:
        rec["note"] = note
    append_log(paths, rec)


# ---------------------------------------------------------------- notifications and logging

def notify(paths, text, quiet=False):
    line = "%s  %s" % (ny_iso(now_ny()), text)
    os.makedirs(os.path.dirname(paths.log), exist_ok=True)
    with open(paths.log, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if not quiet:
        print(line, flush=True)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat and not quiet:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok, data=data, timeout=10).read()
        except Exception as e:  # a failed notification never stops the trading loop
            with open(paths.log, "a", encoding="utf-8") as fh:
                fh.write("%s  telegram failed: %s\n" % (ny_iso(now_ny()), e))


# ---------------------------------------------------------------- the broker adapter

class Alpaca:
    """Thin wrapper around alpaca-py; everything the loop needs, nothing else."""

    def __init__(self, paper, feed="iex"):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        key, sec = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not sec:
            raise AutoError("ALPACA_API_KEY and ALPACA_SECRET_KEY are not in the environment")
        self.trading = TradingClient(key, sec, paper=paper)
        self.data = StockHistoricalDataClient(key, sec)
        self.feed = feed
        self.paper = paper

    def clock(self):
        c = self.trading.get_clock()
        return {"now": c.timestamp.astimezone(NY), "is_open": bool(c.is_open),
                "next_open": c.next_open.astimezone(NY), "next_close": c.next_close.astimezone(NY)}

    def account(self):
        a = self.trading.get_account()
        return {"cash": float(a.cash), "equity": float(a.equity), "buying_power": float(a.buying_power),
                "status": str(a.status)}

    def bars(self, symbols, start, end=None):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from alpaca.data.enums import DataFeed
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=start.astimezone(dt.timezone.utc), end=(end.astimezone(dt.timezone.utc) if end else None),
                               feed=DataFeed(self.feed))
        bs = self.data.get_stock_bars(req)
        out = {}
        for sym, bars in bs.data.items():
            out[sym] = [{"t": b.timestamp.astimezone(NY), "o": float(b.open), "h": float(b.high), "l": float(b.low),
                         "c": float(b.close), "v": float(b.volume)} for b in bars]
        return out

    def submit_bracket(self, symbol, qty, stop, target, client_id):
        from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                 order_class=OrderClass.BRACKET, client_order_id=client_id,
                                 take_profit=TakeProfitRequest(limit_price=target),
                                 stop_loss=StopLossRequest(stop_price=stop))
        o = self.trading.submit_order(req)
        return self._order(o)

    def _order(self, o):
        return {"id": str(o.id), "client_order_id": o.client_order_id, "symbol": o.symbol, "side": str(o.side.value),
                "status": str(o.status.value), "qty": float(o.qty or 0), "filled_qty": float(o.filled_qty or 0),
                "filled_avg_price": (float(o.filled_avg_price) if o.filled_avg_price else None),
                "type": str(o.type.value) if o.type else None,
                "legs": [self._order(l) for l in (o.legs or [])]}

    def order(self, order_id):
        from alpaca.trading.requests import GetOrderByIdRequest
        return self._order(self.trading.get_order_by_id(order_id, GetOrderByIdRequest(nested=True)))

    def open_orders(self, symbol=None):
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, symbols=([symbol] if symbol else None))
        return [self._order(o) for o in self.trading.get_orders(req)]

    def cancel(self, order_id):
        self.trading.cancel_order_by_id(order_id)

    def close_position(self, symbol):
        return self._order(self.trading.close_position(symbol))

    def position(self, symbol):
        """The open position, with the shares not held by open orders, or None when there is none."""
        from alpaca.common.exceptions import APIError
        try:
            p = self.trading.get_open_position(symbol)
        except APIError as e:
            if getattr(e, "status_code", None) == 404:
                return None
            raise
        qty = float(p.qty)
        return {"symbol": p.symbol, "qty": qty, "avg_entry_price": float(p.avg_entry_price),
                "qty_available": float(p.qty_available) if p.qty_available is not None else qty}

    def positions(self):
        return [{"symbol": p.symbol, "qty": float(p.qty), "avg_entry_price": float(p.avg_entry_price)}
                for p in self.trading.get_all_positions()]


# ---------------------------------------------------------------- the session

class Session:
    """One trading day. `broker` is an Alpaca or a fake with the same methods;
    `now_fn` and `sleep_fn` are injectable so a whole day runs in a test."""

    def __init__(self, broker, cfg, auto, paths, paper, now_fn=now_ny, sleep_fn=time.sleep):
        self.b, self.cfg, self.auto, self.paths, self.paper = broker, cfg, auto, paths, paper
        self.now_fn, self.sleep_fn = now_fn, sleep_fn
        self.day = now_fn().date()
        self.date = self.day.isoformat()
        self.open = at_time(self.day, "09:30")
        self.entry_from = at_time(self.day, auto["entry_from"])
        self.entry_until = at_time(self.day, auto["entry_until"])
        self.flat_by = at_time(self.day, cfg.get("flat_by", "15:55"))
        self.close = at_time(self.day, "16:00")
        self.state = read_json(paths.state) or {}
        self.trades = self.state.setdefault(self.date, {})   # symbol -> dict

    def save(self):
        write_json(self.paths.state, self.state)

    def say(self, text, quiet=False):
        notify(self.paths, text, quiet)

    def stop_requested(self):
        return os.path.exists(self.paths.stop)

    def open_risk(self):
        return round(sum(t["max_loss"] for t in self.trades.values()
                         if t.get("entry_filled") and not t.get("exit_filled")), 2)

    # ---- entries

    def scan_once(self, now):
        symbols = [s for s in self.auto["symbols"] if s not in self.trades]
        if not symbols:
            return
        bars = self.b.bars(symbols, self.open, now)
        hits = []
        for sym in symbols:
            sb = bars.get(sym) or []
            orr = opening_range(sb, self.auto["or_minutes"], self.open)
            if not orr:
                continue
            bar = breakout_bar(sb, orr, now, self.open, self.auto["or_minutes"], self.entry_from, self.entry_until,
                               self.auto["min_bar_volume"])
            if bar:
                strength = (bar["c"] - orr["high"]) / orr["high"]
                hits.append((strength, sym, bar, orr))
        for _, sym, bar, orr in sorted(hits, key=lambda x: -x[0]):
            self.try_entry(sym, bar, orr, now)

    def try_entry(self, sym, bar, orr, now):
        plan, why = plan_for(bar, orr, self.auto["target_r"], self.auto["min_stop_distance_pct"])
        if plan is None:
            self.trades[sym] = {"skipped": why, "at": ny_iso(now)}
            self.save()
            self.say("%s: rule fired at %s but no plan: %s" % (sym, hm(bar["t"]), why), quiet=True)
            return
        plan["or_high"], plan["or_minutes"] = orr["high"], self.auto["or_minutes"]
        cash = self.b.account()["cash"]
        qty, limit, _ = size_order(self.cfg, plan["entry"], plan["stop"], cash)
        log = read_json(self.paths.broker_log) or {"orders": []}
        ok, reasons, max_loss = risk_check(self.cfg, log, self.date, now, plan, qty, sym, self.stop_requested(),
                                           self.open_risk())
        if not ok:
            self.trades[sym] = {"blocked": reasons, "at": ny_iso(now), "plan": plan}
            self.save()
            self.say("%s: BLOCK (%s)" % (sym, "; ".join(reasons)), quiet=True)
            return
        client_id = "at-%s-%s" % (self.date.replace("-", ""), sym)
        try:
            o = self.b.submit_bracket(sym, qty, plan["stop"], plan["target"], client_id)
        except Exception as e:
            self.trades[sym] = {"failed": str(e), "at": ny_iso(now), "plan": plan}
            self.save()
            self.say("%s: order FAILED: %s" % (sym, e))
            return
        self.trades[sym] = {"order_id": o["id"], "client_order_id": client_id, "qty": qty, "stop": plan["stop"],
                            "target": plan["target"], "max_loss": max_loss, "plan": plan, "placed_at": ny_iso(now),
                            "entry_filled": False, "exit_filled": False}
        self.save()
        log_placed(self.paths, now, self.date, sym, qty, plan, max_loss, o["id"], self.paper, client_id)
        self.say("%s: BUY %d at ~%.2f, stop %.2f, target %.2f, max loss $%.2f (size by %s). %s"
                 % (sym, qty, plan["entry"], plan["stop"], plan["target"], max_loss, limit,
                    "PAPER" if self.paper else "LIVE"))

    # ---- fills

    def poll_fills(self, now):
        for sym, t in self.trades.items():
            if "order_id" not in t or t.get("exit_filled") or t["order_id"] in NOT_OURS:
                continue
            try:
                o = self.b.order(t["order_id"])
            except Exception as e:
                self.say("%s: could not read order %s: %s" % (sym, t["order_id"], e), quiet=True)
                continue
            if not t.get("entry_filled"):
                if o["status"] == "filled" and o["filled_avg_price"]:
                    t["entry_filled"], t["entry_price"], t["filled_qty"] = True, o["filled_avg_price"], o["filled_qty"]
                    log_fill(self.paths, now, self.date, sym, "buy", int(o["filled_qty"]), o["filled_avg_price"])
                    self.say("%s: filled %d at %.2f" % (sym, int(o["filled_qty"]), o["filled_avg_price"]))
                    self.save()
                elif o["status"] in ("canceled", "expired", "rejected"):
                    t["exit_filled"], t["note"] = True, "entry %s" % o["status"]
                    self.say("%s: entry %s" % (sym, o["status"]))
                    self.save()
                continue
            for leg in o.get("legs", []):
                if leg["status"] == "filled" and leg["filled_avg_price"] and leg["filled_qty"] > 0:
                    which = "stop" if leg.get("type") in ("stop", "stop_limit") else "target"
                    t["exit_filled"], t["exit_price"], t["exit_by"] = True, leg["filled_avg_price"], which
                    log_fill(self.paths, now, self.date, sym, "sell", int(leg["filled_qty"]), leg["filled_avg_price"],
                             note="%s filled" % which)
                    pnl = (leg["filled_avg_price"] - t["entry_price"]) * leg["filled_qty"]
                    self.say("%s: %s filled at %.2f, P&L $%.2f" % (sym, which, leg["filled_avg_price"], pnl))
                    self.save()
                    break

    # ---- flatten

    def flatten(self, now, why="flat by", symbols=None):
        self.poll_fills(now)   # a stop or target may have filled since the last poll
        for sym, t in self.trades.items():
            if "order_id" not in t or t.get("exit_filled") or (symbols is not None and sym not in symbols):
                continue
            try:
                if not t.get("entry_filled"):
                    self.b.cancel(t["order_id"])
                    t["exit_filled"], t["note"] = True, "entry canceled at %s" % why
                    self.say("%s: unfilled entry canceled (%s)" % (sym, why))
                    continue
                if not self.release(sym, t):
                    self.poll_fills(now)
                    if not t.get("exit_filled"):
                        t["exit_filled"], t["note"] = True, "no position left at the broker at %s" % why
                        self.say("%s: no position left at the broker; check the fills by hand" % sym)
                    continue
                o = self.b.close_position(sym)
                price = None
                for _ in range(30):
                    o = self.b.order(o["id"])
                    if o["status"] == "filled" and o["filled_avg_price"]:
                        price = o["filled_avg_price"]
                        break
                    self.sleep_fn(2)
                if price is None:
                    self.say("%s: close order %s not confirmed filled yet; check the broker" % (sym, o["id"]))
                    continue
                t["exit_filled"], t["exit_price"], t["exit_by"] = True, price, "flatten"
                log_fill(self.paths, now, self.date, sym, "sell", int(o["filled_qty"]), price, note="%s" % why)
                self.say("%s: flattened at %.2f (%s), P&L $%.2f" % (sym, price, why, (price - t["entry_price"]) * o["filled_qty"]))
            except Exception as e:
                self.say("%s: flatten FAILED: %s. Close it by hand." % (sym, e))
            finally:
                self.save()

    def release(self, sym, t):
        """Cancel the bracket's stop and target, then wait until the broker frees the shares they hold.
        Alpaca's open-orders list hides the legs of a filled parent, so the legs come from the parent itself,
        and a cancel lands seconds later, not at once. False when the position is already gone."""
        ids = {o["id"] for o in self.b.open_orders(sym)}
        if t["order_id"] not in NOT_OURS:
            ids |= {leg["id"] for leg in self.b.order(t["order_id"]).get("legs", []) if leg["status"] not in DONE}
        for oid in ids:
            try:
                self.b.cancel(oid)
            except Exception as e:
                self.say("%s: cancel of %s failed: %s" % (sym, oid, e), quiet=True)
        for _ in range(RELEASE_WAIT_SECONDS):
            pos = self.b.position(sym)
            if pos is None:
                return False
            if pos["qty_available"] >= pos["qty"] - 1e-9:
                return True
            self.sleep_fn(1)
        raise AutoError("%s of %s shares still held for orders after %d s"
                        % (pos["qty"] - pos["qty_available"], pos["qty"], RELEASE_WAIT_SECONDS))

    def close_leftovers(self, now):
        """A position this session did not open (a failed flatten, a buy by hand) breaks flat-by: close it first."""
        try:
            left = [p for p in self.b.positions() if p["symbol"] not in self.trades]
        except Exception as e:
            self.say("could not read positions: %s" % e)
            return
        if not left:
            return
        for p in left:
            self.trades[p["symbol"]] = {"order_id": "carried", "qty": p["qty"], "filled_qty": p["qty"],
                                        "entry_filled": True, "entry_price": p["avg_entry_price"], "max_loss": 0.0,
                                        "exit_filled": False}
        self.save()
        self.say("%s not opened today; closing at market first" % ", ".join(
            "%s x%g" % (p["symbol"], p["qty"]) for p in left))
        self.flatten(now, why="carried over", symbols=[p["symbol"] for p in left])

    # ---- the day

    def run(self):
        self.say("autotrader %s: %s, %d symbols, entries %s-%s, flat by %s"
                 % (self.date, "PAPER" if self.paper else "LIVE", len(self.auto["symbols"]), self.auto["entry_from"],
                    self.auto["entry_until"], hm(self.flat_by)))
        self.close_leftovers(self.now_fn())
        last_scan_slot = None
        while True:
            now = self.now_fn()
            if self.stop_requested():
                self.say("trader/STOP found: flattening and stopping for today")
                self.flatten(now, why="STOP file")
                break
            if now >= self.flat_by:
                self.flatten(now)
                break
            if self.entry_from <= now <= self.entry_until + dt.timedelta(minutes=1):
                slot = (now.hour, now.minute - now.minute % 5)
                if slot != last_scan_slot and now.second >= 5:
                    last_scan_slot = slot
                    try:
                        self.scan_once(now)
                    except Exception as e:
                        self.say("scan failed: %s" % e, quiet=True)
            live = [t for t in self.trades.values() if "order_id" in t and not t.get("exit_filled")]
            if live:
                self.poll_fills(now)
            elif now > self.entry_until:
                placed = [t for t in self.trades.values() if "order_id" in t]
                self.say("no entries today" if not placed else "every trade is closed and the entry window is over",
                         quiet=True)
                break
            self.sleep_fn(self.auto["poll_seconds"])
        self.summary()

    def summary(self):
        done = [t for t in self.trades.values() if t.get("entry_filled")]
        pnl = sum((t.get("exit_price", t["entry_price"]) - t["entry_price"]) * t.get("filled_qty", t["qty"]) for t in done)
        self.say("%s done: %d entr%s, P&L $%.2f" % (self.date, len(done), "y" if len(done) == 1 else "ies", pnl))
        journal = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude", "skills",
                               "trader-journal", "journal.py")
        if os.path.exists(journal):
            import subprocess
            try:
                out = subprocess.run([sys.executable, journal, "report", "--config", self.paths.config, "--state",
                                      self.paths.broker_log, "--journal", os.path.join(self.paths.root, "journal.json")]
                                     + ([] if self.paper else ["--live"]), capture_output=True, text=True, timeout=60)
                self.say(out.stdout.strip() or out.stderr.strip())
            except Exception as e:
                self.say("journal failed: %s" % e, quiet=True)


# ---------------------------------------------------------------- commands

def cmd_check(a, paths):
    cfg, auto = load_config(paths)
    paper = paper_mode(cfg)
    print("config: budget $%s, max loss/trade $%s, daily $%s, trades/day %s, %s" % (
        cfg.get("budget"), cfg["max_loss_per_trade_usd"], cfg["max_daily_loss_usd"], cfg.get("max_trades_per_day"),
        "PAPER" if paper else "LIVE"))
    print("rule: %d symbols, range %d min, entries %s-%s, target %sR, flat by %s, feed %s" % (
        len(auto["symbols"]), auto["or_minutes"], auto["entry_from"], auto["entry_until"], auto["target_r"],
        cfg.get("flat_by", "15:55"), auto["feed"]))
    b = Alpaca(paper, auto["feed"])
    acct = b.account()
    print("account: %s, cash $%.2f, equity $%.2f, buying power $%.2f" % (acct["status"], acct["cash"], acct["equity"],
                                                                        acct["buying_power"]))
    c = b.clock()
    print("clock: %s New York, market %s, next open %s, next close %s" % (
        c["now"].strftime("%Y-%m-%d %H:%M"), "OPEN" if c["is_open"] else "closed", c["next_open"].strftime("%Y-%m-%d %H:%M"),
        c["next_close"].strftime("%Y-%m-%d %H:%M")))
    sym = auto["symbols"][0]
    start = c["now"] - dt.timedelta(days=5)
    bars = b.bars([sym], start, c["now"])
    n = len(bars.get(sym, []))
    print("data: %d five-minute bars of %s over the last 5 days from %s%s" % (n, sym, auto["feed"], "" if n else "  <- NO DATA"))
    print("telegram: %s" % ("set" if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID") else "not set"))
    return 0 if n else 1


def cmd_day(a, paths):
    cfg, auto = load_config(paths)
    paper = paper_mode(cfg)
    b = Alpaca(paper, auto["feed"])
    c = b.clock()
    if not c["is_open"] and c["next_open"].date() != c["now"].date():
        print("the market is closed today (next open %s); nothing to do" % c["next_open"].strftime("%Y-%m-%d %H:%M"))
        return 0
    Session(b, cfg, auto, paths, paper).run()
    return 0


def cmd_daemon(a, paths):
    while True:
        try:
            cfg, auto = load_config(paths)
            paper = paper_mode(cfg)
            b = Alpaca(paper, auto["feed"])
            c = b.clock()
            now = c["now"]
            session_today = c["is_open"] or c["next_open"].date() == now.date()
            start = at_time(now.date(), auto["entry_from"]) - dt.timedelta(minutes=5)
            if session_today and now < at_time(now.date(), "16:00"):
                if now >= start:
                    Session(b, cfg, auto, paths, paper).run()
                    target = at_time(now.date(), "16:05")
                else:
                    target = start
            else:
                nxt = c["next_open"].date()
                target = at_time(nxt, auto["entry_from"]) - dt.timedelta(minutes=5)
            wait = max(30.0, (target - now_ny()).total_seconds())
            notify(paths, "daemon: sleeping %.0f min until %s" % (wait / 60, target.strftime("%Y-%m-%d %H:%M")), quiet=True)
            time.sleep(min(wait, 1800))
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            notify(paths, "daemon error: %s\n%s" % (e, traceback.format_exc()[-800:]))
            time.sleep(300)


def cmd_flatten(a, paths):
    cfg, auto = load_config(paths)
    paper = paper_mode(cfg)
    b = Alpaca(paper, auto["feed"])
    s = Session(b, cfg, auto, paths, paper)
    for p in b.positions():
        if p["symbol"] not in s.trades:
            s.trades[p["symbol"]] = {"order_id": "manual", "qty": p["qty"], "entry_filled": True,
                                     "entry_price": p["avg_entry_price"], "max_loss": 0.0, "exit_filled": False}
    s.flatten(s.now_fn(), why="flatten command")
    return 0


def cmd_status(a, paths):
    cfg, auto = load_config(paths)
    paper = paper_mode(cfg)
    b = Alpaca(paper, auto["feed"])
    print("positions:", json.dumps(b.positions(), indent=1))
    print("open orders:", json.dumps([{k: o[k] for k in ("id", "symbol", "side", "status", "qty", "type")}
                                      for o in b.open_orders()], indent=1))
    date = now_ny().date().isoformat()
    st = (read_json(paths.state) or {}).get(date, {})
    print("today (%s): %s" % (date, json.dumps(st, indent=1, default=str) if st else "nothing yet"))
    if os.path.exists(paths.stop):
        print("trader/STOP is present: no new entries")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Unattended opening-range-breakout executor for the day-trader project.")
    ap.add_argument("--root", default="trader", help="the trader/ folder (default: ./trader)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("check", "day", "daemon", "flatten", "status"):
        sub.add_parser(name)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    paths = Paths(a.root)
    try:
        return {"check": cmd_check, "day": cmd_day, "daemon": cmd_daemon, "flatten": cmd_flatten,
                "status": cmd_status}[a.cmd](a, paths)
    except AutoError as e:
        print("autotrader: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
