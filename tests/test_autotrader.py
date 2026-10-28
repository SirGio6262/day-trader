import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import ROOT, load

spec = importlib.util.spec_from_file_location("autotrader", os.path.join(ROOT, "autotrader", "autotrader.py"))
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)
journal = load("trader-journal", "journal")

NY = at.NY
DAY = dt.date(2026, 10, 19)


def t(h, m):
    return dt.datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=NY)


def bar(h, m, o, hi, lo, c, v=100000):
    return {"t": t(h, m), "o": o, "h": hi, "l": lo, "c": c, "v": v}


# TSLA: range 354.47-362.18 over 9:30-9:45, first close above the high on the 10:10 bar (done 10:15).
TSLA = [bar(9, 30, 356, 362.18, 354.47, 358), bar(9, 35, 358, 361, 356, 360), bar(9, 40, 360, 361.5, 359, 360.5),
        bar(9, 45, 360.5, 362, 359.5, 361), bar(9, 50, 361, 362.1, 360, 361.9), bar(9, 55, 361.9, 362.0, 360.5, 361),
        bar(10, 0, 361, 362, 360, 361.5), bar(10, 5, 361.5, 362.1, 361, 362), bar(10, 10, 362, 364.5, 361.8, 364.18),
        bar(10, 15, 364.18, 366, 363.5, 365)]
# AAPL: never leaves its range.
AAPL = [bar(9, 30, 250, 252, 249, 251), bar(9, 35, 251, 251.5, 250, 250.5), bar(9, 40, 250.5, 251, 249.5, 250),
        bar(9, 45, 250, 250.8, 249.6, 250.2), bar(9, 50, 250.2, 251, 249.8, 250.9), bar(10, 0, 250.9, 251.9, 250.5, 251.8)]


class FakeBroker:
    """Bars by symbol, orders that fill when told to."""

    def __init__(self, bars, cash=5000.0):
        self.all_bars = bars
        self.cash = cash
        self.orders = {}
        self.n = 0
        self.canceled = []
        self.closed = []
        self.submitted = []

    def account(self):
        return {"cash": self.cash, "equity": self.cash, "buying_power": self.cash, "status": "ACTIVE"}

    def bars(self, symbols, start, end=None):
        return {s: [b for b in self.all_bars.get(s, []) if b["t"] >= start and (end is None or b["t"] < end)] for s in symbols}

    def _new(self, symbol, side, qty, typ, status="new"):
        self.n += 1
        o = {"id": "o%d" % self.n, "client_order_id": None, "symbol": symbol, "side": side, "status": status, "qty": qty,
             "filled_qty": 0.0, "filled_avg_price": None, "type": typ, "legs": []}
        self.orders[o["id"]] = o
        return o

    def submit_bracket(self, symbol, qty, stop, target, client_id):
        o = self._new(symbol, "buy", qty, "market")
        o["client_order_id"] = client_id
        o["legs"] = [self._new(symbol, "sell", qty, "limit", "held"), self._new(symbol, "sell", qty, "stop", "held")]
        o["legs"][0]["limit_price"], o["legs"][1]["stop_price"] = target, stop
        self.submitted.append((symbol, qty, stop, target, client_id))
        return o

    def fill(self, order_id, price, qty=None):
        o = self.orders[order_id]
        o["status"], o["filled_avg_price"], o["filled_qty"] = "filled", price, qty or o["qty"]
        for leg in o["legs"]:
            leg["status"] = "new"

    def order(self, order_id):
        return self.orders[order_id]

    def open_orders(self, symbol=None):
        return [o for o in self.orders.values() if o["status"] in ("new", "held") and (symbol is None or o["symbol"] == symbol)]

    def cancel(self, order_id):
        self.orders[order_id]["status"] = "canceled"
        self.canceled.append(order_id)

    def close_position(self, symbol):
        o = self._new(symbol, "sell", 8, "market")
        self.closed.append(symbol)
        self.fill(o["id"], 360.00)
        return o

    def positions(self):
        return []


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now = self.now + dt.timedelta(seconds=seconds)


class RuleTest(unittest.TestCase):
    def test_opening_range_needs_all_bars(self):
        self.assertIsNone(at.opening_range(TSLA[:2], 15, t(9, 30)))
        orr = at.opening_range(TSLA, 15, t(9, 30))
        self.assertEqual((orr["high"], orr["low"], orr["mid"]), (362.18, 354.47, 358.32))

    def test_breakout_is_the_first_completed_close_above_the_high_inside_the_window(self):
        orr = at.opening_range(TSLA, 15, t(9, 30))
        # At 10:14 the 10:10 bar is not complete.
        self.assertIsNone(at.breakout_bar(TSLA, orr, t(10, 14), t(9, 30), 15, t(9, 50), t(10, 30)))
        b = at.breakout_bar(TSLA, orr, t(10, 15, ), t(9, 30), 15, t(9, 50), t(10, 30))
        self.assertEqual(b["t"], t(10, 10))
        # Window closed before the breakout: nothing.
        self.assertIsNone(at.breakout_bar(TSLA, orr, t(10, 15), t(9, 30), 15, t(9, 50), t(10, 0)))
        # AAPL never breaks out.
        self.assertIsNone(at.breakout_bar(AAPL, at.opening_range(AAPL, 15, t(9, 30)), t(10, 15), t(9, 30), 15, t(9, 50), t(10, 30)))

    def test_plan_matches_the_pack_example(self):
        orr = at.opening_range(TSLA, 15, t(9, 30))
        plan, why = at.plan_for(TSLA[8], orr, 1.0, 0.15)
        self.assertIsNone(why)
        self.assertEqual((plan["entry"], plan["stop"], plan["target"], plan["risk_per_share"]), (364.18, 358.32, 370.04, 5.86))
        plan2, _ = at.plan_for(TSLA[8], orr, 2.0, 0.15)
        self.assertEqual(plan2["target"], 375.9)

    def test_plan_refuses_a_stop_too_tight(self):
        orr = {"high": 100.0, "low": 99.9, "mid": 99.95}
        plan, why = at.plan_for({"t": t(10, 10), "c": 100.01, "v": 1}, orr, 2.0, 0.15)
        self.assertIsNone(plan)
        self.assertIn("too tight", why)

    def test_size_is_the_smallest_limit(self):
        cfg = {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000,
               "min_order_usd": 5, "max_loss_per_trade_usd": 50}
        qty, limit, _ = at.size_order(cfg, 364.18, 358.32, 5000)
        self.assertEqual((qty, limit), (8, "max loss per trade"))
        qty, limit, _ = at.size_order(cfg, 364.18, 300.0, 5000)
        self.assertEqual((qty, limit), (0, "under the minimum order"))   # 50 / 64.18 = 0 shares
        loose = dict(cfg, max_loss_per_trade_usd=5000, max_order_usd=3000)
        qty, limit, _ = at.size_order(loose, 364.18, 358.32, 5000)
        self.assertEqual((qty, limit), (8, "max order"))
        qty, limit, _ = at.size_order(cfg, 364.18, 358.32, 1000)   # cash 1000 minus the 10% budget buffer 500 = 1 share
        self.assertEqual((qty, limit), (1, "cash after the buffer"))


class RiskTest(unittest.TestCase):
    cfg = {"budget": 5000, "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150, "max_trades_per_day": 3,
           "min_reward_risk": 1.0, "no_entries_before": "09:45", "no_entries_after": "15:30", "blocked": ["GME"]}
    plan = {"risk_per_share": 5.86, "reward_risk": 2.0}

    def test_pass(self):
        ok, reasons, loss = at.risk_check(self.cfg, {"orders": []}, "2026-10-19", t(10, 15), self.plan, 8, "TSLA", False, 0.0)
        self.assertTrue(ok, reasons)
        self.assertEqual(loss, 46.88)

    def test_blocks(self):
        log = {"orders": [{"status": "placed", "side": "buy", "symbol": "TSLA", "date": "2026-10-19", "max_loss": 46.88}]}
        ok, reasons, _ = at.risk_check(self.cfg, log, "2026-10-19", t(10, 15), self.plan, 8, "TSLA", False, 46.88)
        self.assertFalse(ok)
        self.assertTrue(any("already traded" in r for r in reasons))
        ok, reasons, _ = at.risk_check(self.cfg, {"orders": []}, "2026-10-19", t(10, 15), self.plan, 8, "GME", False, 0)
        self.assertTrue(any("blocked" in r for r in reasons))
        ok, reasons, _ = at.risk_check(self.cfg, {"orders": []}, "2026-10-19", t(10, 15), self.plan, 8, "TSLA", True, 0)
        self.assertTrue(any("STOP" in r for r in reasons))
        ok, reasons, _ = at.risk_check(self.cfg, {"orders": []}, "2026-10-19", t(15, 40), self.plan, 8, "TSLA", False, 0)
        self.assertTrue(any("outside the entry window" in r for r in reasons))
        ok, reasons, _ = at.risk_check(self.cfg, {"orders": []}, "2026-10-19", t(10, 15), self.plan, 8, "TSLA", False, 120.0)
        self.assertTrue(any("daily limit" in r for r in reasons))
        ok, reasons, _ = at.risk_check(self.cfg, {"orders": []}, "2026-10-19", t(10, 15), self.plan, 9, "TSLA", False, 0)
        self.assertTrue(any("per-trade limit" in r for r in reasons))

    def test_realized_losses_count_against_the_day(self):
        log = {"orders": [
            {"status": "filled", "side": "buy", "symbol": "AMD", "date": "2026-10-19", "qty": "10", "price": 100, "at": "2026-10-19T09:55:00-04:00"},
            {"status": "filled", "side": "sell", "symbol": "AMD", "date": "2026-10-19", "qty": "10", "price": 88, "at": "2026-10-19T10:05:00-04:00"}]}
        self.assertEqual(at.realized_today(log, "2026-10-19"), -120.0)
        ok, reasons, _ = at.risk_check(self.cfg, log, "2026-10-19", t(10, 15), self.plan, 8, "TSLA", False, 0)
        self.assertFalse(ok)
        self.assertTrue(any("daily limit" in r for r in reasons))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = at.Paths(os.path.join(self.dir, "trader"))
        os.makedirs(self.paths.root)
        with open(os.path.join(ROOT, ".claude", "skills", "trader-budget", "config.example.json")) as fh:
            self.cfg = json.load(fh)
        self.cfg["journal"] = {"cost_per_share_round_trip": 0.0}
        at.write_json(self.paths.config, self.cfg)
        self.auto = dict(at.DEFAULTS)
        self.auto.update({"symbols": ["TSLA", "AAPL"], "target_r": 1.0, "poll_seconds": 30})
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_day(self, broker, clock, hooks):
        """hooks: {time -> callable(broker, session)} fired once when the clock passes that time."""
        s = at.Session(broker, self.cfg, self.auto, self.paths, True, now_fn=clock, sleep_fn=clock.sleep)
        fired = set()
        real_sleep = clock.sleep

        def sleep(seconds):
            real_sleep(seconds)
            for when, fn in hooks.items():
                if when not in fired and clock.now >= when:
                    fired.add(when)
                    fn(broker, s)
        s.sleep_fn = sleep
        with redirect_stdout(io.StringIO()):
            s.run()
        return s

    def test_a_full_day_target_hit(self):
        broker = FakeBroker({"TSLA": TSLA, "AAPL": AAPL})
        clock = Clock(t(9, 46))

        def fill_entry(b, s):
            b.fill(s.trades["TSLA"]["order_id"], 364.20)

        def fill_target(b, s):
            o = b.orders[s.trades["TSLA"]["order_id"]]
            b.fill(o["legs"][0]["id"], 370.04)

        s = self.run_day(broker, clock, {t(10, 16): fill_entry, t(11, 40): fill_target})
        self.assertEqual(len(broker.submitted), 1)
        sym, qty, stop, target, cid = broker.submitted[0]
        self.assertEqual((sym, qty, stop, target, cid), ("TSLA", 8, 358.32, 370.04, "at-20261019-TSLA"))
        self.assertTrue(s.trades["TSLA"]["exit_filled"])
        self.assertEqual(s.trades["TSLA"]["exit_by"], "target")
        self.assertNotIn("order_id", s.trades.get("AAPL", {}))
        self.assertEqual(broker.closed, [])     # nothing left to flatten
        log = at.read_json(self.paths.broker_log)
        statuses = [(r["status"], r.get("side")) for r in log["orders"]]
        self.assertEqual(statuses, [("approved", "buy"), ("placed", "buy"), ("filled", "buy"), ("filled", "sell")])
        trades, still_open = journal.trades_from_broker_log(log, 0.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(still_open, {})
        self.assertAlmostEqual(trades[0]["r"], (370.04 - 364.20) / (364.20 - 358.32), places=2)
        self.assertEqual(trades[0]["stop"], 358.32)

    def test_flat_by_closes_what_is_still_open(self):
        broker = FakeBroker({"TSLA": TSLA})
        clock = Clock(t(9, 46))
        s = self.run_day(broker, clock, {t(10, 16): lambda b, s: b.fill(s.trades["TSLA"]["order_id"], 364.20)})
        self.assertEqual(broker.closed, ["TSLA"])
        self.assertEqual(s.trades["TSLA"]["exit_by"], "flatten")
        self.assertGreaterEqual(clock.now, t(15, 55))
        legs = broker.orders[s.trades["TSLA"]["order_id"]]["legs"]
        self.assertTrue(all(l["id"] in broker.canceled for l in legs))
        log = at.read_json(self.paths.broker_log)
        self.assertEqual(log["orders"][-1]["side"], "sell")
        self.assertEqual(log["orders"][-1]["price"], 360.0)

    def test_stop_file_flattens_and_stops(self):
        broker = FakeBroker({"TSLA": TSLA})
        clock = Clock(t(9, 46))

        def stop(b, s):
            with open(self.paths.stop, "w") as fh:
                fh.write("stop\n")

        s = self.run_day(broker, clock, {t(10, 16): lambda b, s: b.fill(s.trades["TSLA"]["order_id"], 364.20),
                                         t(10, 30): stop})
        self.assertEqual(broker.closed, ["TSLA"])
        self.assertLess(clock.now, t(11, 0))

    def test_restart_does_not_enter_twice(self):
        broker = FakeBroker({"TSLA": TSLA})
        clock = Clock(t(9, 46))
        s = at.Session(broker, self.cfg, self.auto, self.paths, True, now_fn=clock, sleep_fn=clock.sleep)
        with redirect_stdout(io.StringIO()):
            s.scan_once(t(10, 15))
        self.assertEqual(len(broker.submitted), 1)
        s2 = at.Session(broker, self.cfg, self.auto, self.paths, True, now_fn=clock, sleep_fn=clock.sleep)
        with redirect_stdout(io.StringIO()):
            s2.scan_once(t(10, 20))
        self.assertEqual(len(broker.submitted), 1)
        self.assertIn("TSLA", s2.trades)

    def test_trades_per_day_limit_holds_across_symbols(self):
        self.cfg["max_trades_per_day"] = 1
        at.write_json(self.paths.config, self.cfg)
        nvda = [dict(b, t=b["t"]) for b in TSLA]
        broker = FakeBroker({"TSLA": TSLA, "NVDA": nvda})
        self.auto["symbols"] = ["TSLA", "NVDA"]
        clock = Clock(t(9, 46))
        s = at.Session(broker, self.cfg, self.auto, self.paths, True, now_fn=clock, sleep_fn=clock.sleep)
        with redirect_stdout(io.StringIO()):
            s.scan_once(t(10, 15))
        self.assertEqual(len(broker.submitted), 1)
        blocked = [k for k, v in s.trades.items() if "blocked" in v]
        self.assertEqual(len(blocked), 1)

    def test_paper_mismatch_is_refused(self):
        os.environ["ALPACA_PAPER_TRADE"] = "false"
        try:
            with self.assertRaises(at.AutoError):
                at.paper_mode({"paper": True})
        finally:
            os.environ["ALPACA_PAPER_TRADE"] = "true"
        self.assertTrue(at.paper_mode({"paper": True}))

    def test_config_refuses_shorts_options_and_no_stop(self):
        for k in ("allow_short", "allow_options", "allow_crypto"):
            cfg = dict(self.cfg)
            cfg[k] = True
            at.write_json(self.paths.config, cfg)
            with self.assertRaises(at.AutoError):
                at.load_config(self.paths)
        cfg = dict(self.cfg)
        cfg["require_stop"] = False
        at.write_json(self.paths.config, cfg)
        with self.assertRaises(at.AutoError):
            at.load_config(self.paths)


if __name__ == "__main__":
    unittest.main()
