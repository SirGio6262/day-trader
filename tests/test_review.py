import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

review = load("trader-journal", "review")

CFG = {"budget": 5000, "blocked": [], "journal": {"cost_per_share_round_trip": 0.0, "min_trades": 60}}


def trade_rows(sym, date, hm, entry, stop, exit_price, note, qty=10, features=None, mfe=None):
    at_in = "%sT%s:00-04:00" % (date, hm)
    at_out = "%sT15:00:00-04:00" % date
    placed = {"at": at_in, "date": date, "status": "placed", "symbol": sym, "side": "buy", "qty": str(qty),
              "stop": stop, "target": entry + 2 * (entry - stop), "paper": True}
    if features:
        placed["features"] = features
    sell = {"at": at_out, "date": date, "status": "filled", "symbol": sym, "side": "sell", "qty": str(qty),
            "price": exit_price, "note": note}
    if mfe is not None:
        sell["excursion"] = {"mfe_r": mfe, "mae_r": 1.0, "bars": 10}
    return [placed, {"at": at_in, "date": date, "status": "filled", "symbol": sym, "side": "buy", "qty": str(qty),
                     "price": entry}, sell]


def state_of(*groups):
    return {"orders": [r for g in groups for r in g]}


class ExitKindTest(unittest.TestCase):
    def test_notes(self):
        self.assertEqual(review.exit_kind("stop filled"), "stop")
        self.assertEqual(review.exit_kind("target partial fill"), "target")
        self.assertEqual(review.exit_kind("STOP file"), "flatten")
        self.assertEqual(review.exit_kind("flat by"), "flatten")
        self.assertEqual(review.time_bucket("10:12"), "10:10–10:19")


class ReportTest(unittest.TestCase):
    def test_matches_journal_report(self):
        st = state_of(trade_rows("AAPL", "2026-11-03", "09:55", 100, 99, 102, "target filled"),
                      trade_rows("MSFT", "2026-11-03", "10:05", 100, 99, 99, "stop filled"))
        rep = review.build_report(st, CFG)
        self.assertEqual(rep["stats"]["trades"], 2)
        self.assertEqual(rep["stats"]["expectancy_r"], 0.5)
        self.assertEqual(rep["verdict"], "NOT YET")

    def test_enrich_joins_features_and_excursion(self):
        st = state_of(trade_rows("AAPL", "2026-11-03", "09:55", 100, 99, 99, "stop filled",
                                 features={"gap_pct": 0.4}, mfe=1.3))
        rep = review.build_report(st, CFG)
        e = review.enrich(rep["trades"], st)[0]
        self.assertEqual(e["features"]["gap_pct"], 0.4)
        self.assertEqual(e["excursion"]["mfe_r"], 1.3)
        self.assertEqual(e["exit_kind"], "stop")
        self.assertEqual(e["entry_hm"], "09:55")


class DiffTest(unittest.TestCase):
    def test_diff_and_verdict_change(self):
        prev = {"stats": {"trades": 10, "expectancy_r": 0.1, "win_rate": 0.5}, "verdict": "NOT YET", "reasons": ["a"]}
        cur = {"stats": {"trades": 12, "expectancy_r": -0.05, "win_rate": 0.4}, "verdict": "STOP", "reasons": ["a", "b"]}
        d = review.diff(cur, prev)
        row = {r["key"]: r for r in d["rows"]}
        self.assertEqual(row["trades"]["delta"], 2)
        self.assertAlmostEqual(row["expectancy_r"]["delta"], -0.15)
        self.assertTrue(d["verdict_changed"])
        self.assertEqual(d["reasons_new"], ["b"])

    def test_first_review_has_no_prev(self):
        d = review.diff({"stats": {"trades": 1}, "verdict": "NOT YET", "reasons": []}, None)
        self.assertFalse(d["verdict_changed"])
        self.assertIsNone(d["verdict_prev"])


class GapTest(unittest.TestCase):
    def _trades(self, st):
        return review.enrich(review.build_report(st, CFG)["trades"], st)

    def test_symbol_dragging_is_the_gap(self):
        groups = []
        for i in range(4):
            groups.append(trade_rows("JPM", "2026-11-%02d" % (3 + i), "09:55", 100, 99, 99, "stop filled"))
            groups.append(trade_rows("AAPL", "2026-11-%02d" % (3 + i), "09:55", 100, 99, 101, "flat by"))
        gaps = review.find_gaps(self._trades(state_of(*groups)))
        self.assertEqual(gaps[0]["kind"], "symbol")
        self.assertEqual(gaps[0]["label"], "JPM")
        self.assertAlmostEqual(gaps[0]["cost_r"], 8.0)   # 4 trades x (1 - (-1))

    def test_giveback_counts_mfe_over_one_r_that_lost(self):
        groups = [trade_rows("AAPL", "2026-11-03", "09:55", 100, 99, 99, "stop filled", mfe=1.4),
                  trade_rows("MSFT", "2026-11-03", "09:55", 100, 99, 99, "stop filled", mfe=0.2),
                  trade_rows("NVDA", "2026-11-03", "09:55", 100, 99, 102, "target filled", mfe=2.0)]
        gaps = review.find_gaps(self._trades(state_of(*groups)))
        gb = [g for g in gaps if g["kind"] == "giveback"][0]
        self.assertEqual(gb["n"], 1)
        self.assertEqual(gb["n_total"], 3)
        self.assertAlmostEqual(gb["cost_r"], 1.0)

    def test_small_groups_are_not_gaps(self):
        groups = [trade_rows("JPM", "2026-11-03", "09:55", 100, 99, 99, "stop filled"),
                  trade_rows("AAPL", "2026-11-03", "09:55", 100, 99, 101, "flat by")]
        self.assertEqual(review.find_gaps(self._trades(state_of(*groups))), [])


class ProposalTest(unittest.TestCase):
    def test_symbol_proposal_is_a_blocked_diff_and_a_hypothesis_under_30(self):
        gap = {"kind": "symbol", "key": "JPM", "label": "JPM", "n": 4, "n_total": 8}
        p = review.make_proposal(gap, CFG, 8, "NOT YET")
        self.assertEqual(p["path"], "blocked")
        self.assertTrue(p["hypothesis"])
        self.assertIn('"JPM"', p["diff"])

    def test_last_time_bucket_moves_entry_until(self):
        gap = {"kind": "time", "key": ("10:20", "10:30"), "label": "10:20–10:30", "n": 10, "n_total": 40}
        p = review.make_proposal(gap, {"autotrader": {"entry_until": "10:30"}}, 40, "NOT YET")
        self.assertEqual(p["path"], "autotrader.entry_until")
        self.assertFalse(p["hypothesis"])
        self.assertIn('"10:19"', p["diff"])

    def test_no_gap_no_proposal(self):
        self.assertIsNone(review.make_proposal(None, CFG, 10, "NOT YET"))

    def test_never_touches_the_journal_block(self):
        for gap in ({"kind": "symbol", "key": "X", "label": "X", "n": 3, "n_total": 6},
                    {"kind": "time", "key": ("09:45", "09:59"), "label": "", "n": 3, "n_total": 6},
                    {"kind": "feature", "key": ("gap_pct", "low", 0.1), "label": "", "n": 3, "n_total": 6},
                    {"kind": "giveback", "key": 1.0, "label": "", "n": 1, "n_total": 6}):
            p = review.make_proposal(gap, CFG, 6, "NOT YET")
            self.assertFalse((p["path"] or "").startswith("journal"))
            self.assertNotIn("journal", p["diff"] or "")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "broker.json")
        self.cfg = os.path.join(self.dir, "config.json")
        self.reviews = os.path.join(self.dir, "reviews")
        with open(self.cfg, "w") as fh:
            json.dump(CFG, fh)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, date, rows):
        with open(self.state, "w") as fh:
            json.dump(state_of(*rows), fh)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = review.main(["run", "--date", date, "--state", self.state, "--config", self.cfg,
                                "--journal", os.path.join(self.dir, "none.json"), "--reviews", self.reviews])
        return code, out.getvalue(), err.getvalue()

    def test_two_days_write_notes_and_diff(self):
        day1 = [trade_rows("AAPL", "2026-11-03", "09:55", 100, 99, 102, "target filled")]
        code, out, err = self.run_cli("2026-11-03", day1)
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.reviews, "2026-11-03.md"), encoding="utf-8") as fh:
            self.assertIn("Прошлого разбора нет", fh.read())
        day2 = day1 + [trade_rows("MSFT", "2026-11-04", "10:05", 100, 99, 99, "stop filled")]
        code, out, err = self.run_cli("2026-11-04", day2)
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.reviews, "2026-11-04.md"), encoding="utf-8") as fh:
            note = fh.read()
        self.assertIn("| закрытых сделок | 1 | 2 (+1) |", note)
        self.assertIn("MSFT", note)
        with open(os.path.join(self.reviews, "2026-11-04.json"), encoding="utf-8") as fh:
            snap = json.load(fh)
        self.assertEqual(snap["stats"]["trades"], 2)
        self.assertNotIn("trades", snap)   # the snapshot keeps the numbers, not the trade list

    def test_config_is_never_written(self):
        with open(self.cfg) as fh:
            before = fh.read()
        self.run_cli("2026-11-03", [trade_rows("AAPL", "2026-11-03", "09:55", 100, 99, 99, "stop filled")])
        with open(self.cfg) as fh:
            self.assertEqual(fh.read(), before)

    def test_missing_state_is_an_error(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = review.main(["run", "--state", os.path.join(self.dir, "nope.json"), "--reviews", self.reviews])
        self.assertEqual(code, 2)

    def test_pull_only_reads_the_remote(self):
        cmds = review.pull_commands("root@h", "/repo", "out")
        self.assertEqual(len(cmds), 2)
        for c in cmds:
            self.assertEqual(c[0], "scp")
            self.assertTrue(c[-2].startswith("root@h:/repo/trader/"))
            self.assertFalse(c[-1].startswith("root@"))


if __name__ == "__main__":
    unittest.main()
