import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

journal = load("trader-journal", "journal")


def rec(at, status, symbol, side, qty, price=None, stop=None, paper=True):
    r = {"at": at, "date": at[:10], "status": status, "symbol": symbol, "side": side, "qty": str(qty), "paper": paper}
    if price is not None:
        r["price"] = price
    if stop is not None:
        r["stop"] = stop
    return r


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = journal.main(argv)
    return code, out.getvalue(), err.getvalue()


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.json")
        self.state = os.path.join(self.dir, "broker.json")
        self.jrn = os.path.join(self.dir, "journal.json")
        with open(self.config, "w") as fh:
            json.dump({"budget": 5000, "journal": {"cost_per_share_round_trip": 0.0}}, fh)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write_state(self, rows):
        with open(self.state, "w") as fh:
            json.dump({"orders": rows}, fh)

    def read_state(self):
        with open(self.state) as fh:
            return json.load(fh)

    def common(self):
        return ["--config", self.config, "--state", self.state, "--journal", self.jrn]

    # ---- pairing fills from the broker log

    def test_pairs_buy_and_sell_with_the_approved_stop(self):
        self.write_state([
            rec("2026-10-19T10:15:00-04:00", "approved", "TSLA", "buy", 8, stop=358.32),
            rec("2026-10-19T10:15:30-04:00", "placed", "TSLA", "buy", 8, stop=358.32),
            rec("2026-10-19T10:16:00-04:00", "filled", "TSLA", "buy", 8, price=364.18),
            rec("2026-10-19T11:40:00-04:00", "filled", "TSLA", "sell", 8, price=370.04),
        ])
        trades, still_open = journal.trades_from_broker_log(self.read_state(), 0.0)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["symbol"], "TSLA")
        self.assertEqual(t["stop"], 358.32)
        self.assertAlmostEqual(t["pnl"], 46.88, places=2)
        self.assertAlmostEqual(t["r"], 1.0, places=2)
        self.assertEqual(still_open, {})

    def test_partial_exit_splits_the_lot_and_reports_what_is_open(self):
        self.write_state([
            rec("2026-10-19T10:15:00-04:00", "approved", "TSLA", "buy", 8, stop=358.32),
            rec("2026-10-19T10:16:00-04:00", "filled", "TSLA", "buy", 8, price=364.18),
            rec("2026-10-19T11:40:00-04:00", "filled", "TSLA", "sell", 5, price=367.00),
        ])
        trades, still_open = journal.trades_from_broker_log(self.read_state(), 0.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["qty"], 5)
        self.assertEqual(still_open, {"TSLA": 3})

    def test_costs_come_off_every_trade(self):
        t = journal.trade("TSLA", "2026-10-19", 8, 364.18, 364.18, 358.32, 0.02, "journal")
        self.assertAlmostEqual(t["pnl_gross"], 0.0)
        self.assertAlmostEqual(t["pnl"], -0.16)
        self.assertLess(t["r"], 0)

    def test_fill_without_a_stop_has_no_r(self):
        self.write_state([
            rec("2026-10-19T10:16:00-04:00", "filled", "AAPL", "buy", 10, price=200.0),
            rec("2026-10-19T15:50:00-04:00", "filled", "AAPL", "sell", 10, price=202.0),
        ])
        trades, _ = journal.trades_from_broker_log(self.read_state(), 0.0)
        self.assertEqual(trades[0]["pnl"], 20.0)
        self.assertIsNone(trades[0]["r"])

    # ---- statistics

    def test_stats_and_drawdown(self):
        trades = [journal.trade("X", "2026-10-%02d" % d, 10, 100, 100 + dx, 99, 0.0, "journal")
                  for d, dx in enumerate([2, -1, -1, 3, -1, 2], start=1)]
        s = journal.stats(trades, budget=1000)
        self.assertEqual(s["trades"], 6)
        self.assertEqual(s["wins"], 3)
        self.assertEqual(s["losses"], 3)
        self.assertAlmostEqual(s["pnl_total"], 40.0)
        self.assertAlmostEqual(s["profit_factor"], 70 / 30, places=3)
        self.assertAlmostEqual(s["expectancy_r"], (2 - 1 - 1 + 3 - 1 + 2) / 6, places=3)
        self.assertEqual(s["max_drawdown"], 20.0)       # +20, then -10, -10
        self.assertEqual(s["max_drawdown_pct"], 2.0)
        self.assertEqual(s["max_consecutive_losses"], 2)
        self.assertIsNotNone(s["t_r"])

    # ---- the gate

    def test_verdict_not_yet_under_the_sample_size(self):
        trades = [journal.trade("X", "2026-10-01", 10, 100, 103, 99, 0.0, "journal")] * 10
        v, reasons = journal.verdict(journal.stats(trades, 1000), journal.DEFAULT_GATE, 1000)
        self.assertEqual(v, "NOT YET")
        self.assertIn("needs 60", reasons[0])

    def test_verdict_go_when_every_bar_clears(self):
        pattern = [2, -1, 2, -1, 1.5, -1]
        trades = [journal.trade("X", "2026-10-01", 10, 100, 100 + dx, 99, 0.0, "journal")
                  for dx in pattern * 12]  # 72 trades, mean 0.417 R
        s = journal.stats(trades, 5000)
        v, reasons = journal.verdict(s, journal.DEFAULT_GATE, 5000)
        self.assertEqual(v, "GO", reasons)

    def test_verdict_stop_on_a_clear_loser(self):
        pattern = [1, -1, -1, 0.5, -1, -1]
        trades = [journal.trade("X", "2026-10-01", 10, 100, 100 + dx, 99, 0.0, "journal") for dx in pattern * 6]
        v, reasons = journal.verdict(journal.stats(trades, 5000), journal.DEFAULT_GATE, 5000)
        self.assertEqual(v, "STOP", reasons)

    def test_verdict_not_yet_when_one_bar_fails(self):
        # Positive and significant, but a 1.05 profit factor: a wash after costs.
        pattern = [1.0, -0.9, 1.0, -0.9, 1.0, -0.95]
        trades = [journal.trade("X", "2026-10-01", 10, 100, 100 + dx, 99, 0.0, "journal") for dx in pattern * 20]
        v, reasons = journal.verdict(journal.stats(trades, 5000), journal.DEFAULT_GATE, 5000)
        self.assertEqual(v, "NOT YET")
        self.assertTrue(any("profit factor" in r and "under" in r for r in reasons), reasons)

    # ---- commands

    def test_add_then_report_and_csv(self):
        code, out, err = run(["add"] + self.common() + ["--symbol", "tsla", "--date", "2026-10-19", "--qty", "8",
                                                        "--entry", "364.18", "--exit", "358.32", "--stop", "358.32",
                                                        "--setup", "orb"])
        self.assertEqual(code, 0, err)
        self.assertIn("-1.0 R", out)
        code, out, _ = run(["report"] + self.common())
        self.assertEqual(code, 0)
        self.assertIn("1 closed paper trade", out)
        self.assertIn("Verdict: NOT YET", out)
        code, out, _ = run(["report", "--json"] + self.common())
        doc = json.loads(out)
        self.assertEqual(doc["stats"]["trades"], 1)
        self.assertIn("orb", doc["by_setup"])
        path = os.path.join(self.dir, "out", "j.csv")
        code, out, _ = run(["csv", path] + self.common())
        self.assertEqual(code, 0)
        with open(path) as fh:
            self.assertEqual(len(fh.read().strip().splitlines()), 2)

    def test_add_refuses_a_stop_above_the_entry(self):
        code, out, err = run(["add"] + self.common() + ["--symbol", "TSLA", "--date", "2026-10-19", "--qty", "8",
                                                        "--entry", "364.18", "--exit", "370", "--stop", "365"])
        self.assertEqual(code, 2)
        self.assertIn("below --entry", err)

    def test_live_and_paper_are_kept_apart(self):
        run(["add"] + self.common() + ["--symbol", "A", "--date", "2026-10-19", "--qty", "1", "--entry", "10",
                                       "--exit", "11", "--stop", "9"])
        run(["add", "--live"] + self.common() + ["--symbol", "B", "--date", "2026-10-20", "--qty", "1",
                                                 "--entry", "10", "--exit", "9", "--stop", "9"])
        _, out, _ = run(["report", "--json"] + self.common())
        self.assertEqual(json.loads(out)["stats"]["trades"], 1)
        _, out, _ = run(["report", "--live", "--json"] + self.common())
        self.assertEqual(json.loads(out)["stats"]["trades"], 1)
        _, out, _ = run(["report", "--all", "--json"] + self.common())
        self.assertEqual(json.loads(out)["stats"]["trades"], 2)

    def test_list_with_nothing(self):
        code, out, _ = run(["list"] + self.common())
        self.assertEqual(code, 1)
        self.assertIn("No closed trades", out)


if __name__ == "__main__":
    unittest.main()
