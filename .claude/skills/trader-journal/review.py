#!/usr/bin/env python3
"""Nightly review for trader-journal: one note a day, one gap, at most one proposal. Standard library only.

After the close, read the day's record, compare it with yesterday's, find the single biggest gap between what
the trades showed and how they ended, and write it down in Russian, numbers first. Nothing here trades, nothing
here edits the config: a proposal is a diff in the note, for Sergey to accept or not, in writing.

    python3 review.py pull                     # read-only scp of broker.json and config.json from the VPS
    python3 review.py run                      # trader/reviews/YYYY-MM-DD.md and .json for today (New York)
    python3 review.py run --date 2026-11-02 --state some/broker.json --config some/config.json

The gap is measured in R and every candidate is scored the same way: how much total R it cost against a
plain counterfactual.
  - a group of trades (a symbol, an entry-time bucket, the low or high half of one entry feature) costs
    n * (mean R of the other trades - mean R of the group): what it would have made had it traded like the rest;
  - give-back: trades that ran to +1R or more (mfe_r) and still closed at a loss cost the R they lost, the
    saving a break-even stop after +1R would have bought (bar data, so the order inside a bar is unknown).
The biggest positive cost wins. Under 30 closed trades any proposal is a hypothesis for backtest.py, not a change.
"""

import argparse
import datetime
import glob
import importlib.util
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VPS_HOST = "root@66.135.4.248"
VPS_REPO = "/home/trader/day-trader"
HYPOTHESIS_BELOW = 30   # closed trades: under this, a proposal is a hypothesis to test, never a change
MIN_GROUP = 3           # trades in a group (and in the rest) before the group can be the gap
GIVEBACK_MFE_R = 1.0
TIME_BUCKETS = (("09:45", "09:59"), ("10:00", "10:09"), ("10:10", "10:19"), ("10:20", "10:30"))
FEATURES = ("or_width_pct", "bar_rel_volume", "break_pct", "bar_range_pct", "close_in_bar",
            "minutes_after_range", "gap_pct")
FEATURE_RU = {"or_width_pct": "ширина диапазона, %", "bar_rel_volume": "отн. объём бара пробоя",
              "break_pct": "пробой над high, %", "bar_range_pct": "размах бара пробоя, %",
              "close_in_bar": "положение close в баре", "minutes_after_range": "минут после диапазона",
              "gap_pct": "гэп от вчерашнего close, %"}


def _load_journal():
    spec = importlib.util.spec_from_file_location("trader_journal_for_review", os.path.join(HERE, "journal.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


J = _load_journal()


class ReviewError(Exception):
    pass


# ---------------------------------------------------------------- the record

def build_report(state, cfg, journal_doc=None):
    """The same numbers `journal.py report --json` prints for paper trades, plus the trade list."""
    gate = J.gate_from(cfg)
    cost = gate["cost_per_share_round_trip"]
    log_trades, still_open = J.trades_from_broker_log(state, cost, True)
    manual = J.trades_from_journal(journal_doc, cost, True)
    trades = sorted(log_trades + manual, key=lambda t: (t["date"], t.get("exit_at") or "", t["symbol"]))
    budget = J._f(cfg.get("budget"))
    s = J.stats(trades, budget)
    v, reasons = J.verdict(s, gate, budget)
    return {"scope": "paper", "gate": gate, "stats": s, "by_symbol": J.by_key(trades, "symbol"),
            "still_open": still_open, "verdict": v, "reasons": reasons, "trades": trades}


def exit_kind(note):
    """autotrader's sell notes: "stop filled", "target filled", "stop partial fill", "target partial fill";
    anything else ("flat by", "carried over", "STOP file", ...) is a market close."""
    n = (note or "").lower()
    for kind in ("stop", "target"):
        if n.startswith(kind + " filled") or n.startswith(kind + " partial"):
            return kind
    return "flatten"


def time_bucket(hm):
    for lo, hi in TIME_BUCKETS:
        if lo <= hm <= hi:
            return "%s–%s" % (lo, hi)
    return "другое"


def enrich(trades, state):
    """Join each broker-log trade with its entry features (the placed buy) and its exit (the sell fill:
    note and excursion). A trade without them keeps None: nothing is guessed."""
    orders = state.get("orders", [])
    placed = {}
    for r in orders:
        if r.get("status") == "placed" and str(r.get("side", "")).lower() == "buy":
            placed.setdefault((str(r.get("symbol", "")).upper(), r.get("date") or str(r.get("at", ""))[:10]), []).append(r)
    sells = {}
    for r in orders:
        if r.get("status") == "filled" and str(r.get("side", "")).lower() == "sell":
            sells[(str(r.get("symbol", "")).upper(), str(r.get("at", "")))] = r
    out = []
    for t in trades:
        e = dict(t)
        p = (placed.get((t["symbol"], t["date"])) or [None])[-1]
        s = sells.get((t["symbol"], t.get("exit_at") or ""))
        e["features"] = (p or {}).get("features")
        e["target"] = J._f((p or {}).get("target"))
        e["exit_note"] = (s or {}).get("note")
        e["exit_kind"] = exit_kind(e["exit_note"]) if s else None
        e["excursion"] = (s or {}).get("excursion")
        hm = (t.get("entry_at") or "")[11:16]
        e["entry_hm"] = hm or None
        e["time_bucket"] = time_bucket(hm) if hm else None
        out.append(e)
    return out


# ---------------------------------------------------------------- yesterday

DIFF_KEYS = (("trades", "закрытых сделок"), ("expectancy_r", "ожидание, R"), ("t_r", "t"),
             ("win_rate", "доля прибыльных"), ("profit_factor", "profit factor"),
             ("max_drawdown", "макс. просадка, $"), ("max_drawdown_pct", "макс. просадка, % бюджета"),
             ("max_consecutive_losses", "худшая серия убытков"))


def diff(cur, prev):
    rows = []
    ps = (prev or {}).get("stats") or {}
    for k, ru in DIFF_KEYS:
        a, b = ps.get(k), cur["stats"].get(k)
        d = None
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            d = round(b - a, 4)
        rows.append({"key": k, "ru": ru, "prev": a, "cur": b, "delta": d})
    pv = (prev or {}).get("verdict")
    pr = set((prev or {}).get("reasons") or [])
    return {"rows": rows, "verdict_prev": pv, "verdict_cur": cur["verdict"],
            "verdict_changed": prev is not None and pv != cur["verdict"],
            "reasons_new": [r for r in cur["reasons"] if r not in pr] if prev else list(cur["reasons"])}


def previous_snapshot(reviews_dir, date):
    best = None
    for p in glob.glob(os.path.join(reviews_dir, "????-??-??.json")):
        d = os.path.basename(p)[:10]
        if d < date and (best is None or d > best[0]):
            best = (d, p)
    if not best:
        return None
    with open(best[1], encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- the gap

def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def welch_t(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = _mean(a), _mean(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return round((ma - mb) / se, 2) if se > 0 else None


def _group_gap(kind, label, key, inside, rest, n_total, extra=None):
    a = [t["r"] for t in inside]
    b = [t["r"] for t in rest]
    ma, mb = _mean(a), _mean(b)
    g = {"kind": kind, "label": label, "key": key, "n": len(a), "n_rest": len(b), "n_total": n_total,
         "mean_r": round(ma, 3), "rest_mean_r": round(mb, 3), "sum_r": round(sum(a), 3),
         "t": welch_t(a, b), "cost_r": round(len(a) * (mb - ma), 3), "days": len({t["date"] for t in inside})}
    g.update(extra or {})
    return g


def _split(trades, pred):
    inside = [t for t in trades if pred(t)]
    rest = [t for t in trades if not pred(t)]
    return inside, rest


def find_gaps(trades):
    """Every candidate gap with its cost in R, biggest first. Only trades with an R count."""
    ts = [t for t in trades if t.get("r") is not None]
    n = len(ts)
    out = []
    for sym in sorted({t["symbol"] for t in ts}):
        inside, rest = _split(ts, lambda t, s=sym: t["symbol"] == s)
        if len(inside) >= MIN_GROUP and len(rest) >= MIN_GROUP:
            out.append(_group_gap("symbol", sym, sym, inside, rest, n))
    for lo, hi in TIME_BUCKETS:
        lab = "%s–%s" % (lo, hi)
        inside, rest = _split([t for t in ts if t.get("time_bucket")], lambda t, l=lab: t["time_bucket"] == l)
        if len(inside) >= MIN_GROUP and len(rest) >= MIN_GROUP:
            out.append(_group_gap("time", lab, (lo, hi), inside, rest, len(inside) + len(rest)))
    for f in FEATURES:
        have = [t for t in ts if isinstance((t.get("features") or {}).get(f), (int, float))]
        if len(have) < 2 * MIN_GROUP:
            continue
        vals = sorted(t["features"][f] for t in have)
        med = vals[len(vals) // 2]
        for half, pred in (("low", lambda t, f=f, m=med: t["features"][f] < m),
                           ("high", lambda t, f=f, m=med: t["features"][f] >= m)):
            inside, rest = _split(have, pred)
            if len(inside) >= MIN_GROUP and len(rest) >= MIN_GROUP:
                lab = "%s %s %s" % (f, "<" if half == "low" else ">=", med)
                out.append(_group_gap("feature", lab, (f, half, med), inside, rest, len(have),
                                      {"n_features_tested": sum(1 for _ in FEATURES)}))
    exc = [t for t in ts if isinstance((t.get("excursion") or {}).get("mfe_r"), (int, float))]
    gb = [t for t in exc if t["excursion"]["mfe_r"] >= GIVEBACK_MFE_R and t["r"] < 0]
    if gb:
        out.append({"kind": "giveback", "label": "дошли до +%gR и закрылись в минусе" % GIVEBACK_MFE_R,
                    "key": GIVEBACK_MFE_R, "n": len(gb), "n_rest": len(exc) - len(gb), "n_total": len(exc),
                    "mean_r": round(_mean([t["r"] for t in gb]), 3),
                    "mean_mfe_r": round(_mean([t["excursion"]["mfe_r"] for t in gb]), 3),
                    "sum_r": round(sum(t["r"] for t in gb), 3), "t": None,
                    "cost_r": round(-sum(t["r"] for t in gb), 3), "days": len({t["date"] for t in gb}),
                    "symbols": [t["symbol"] + " " + t["date"][5:] for t in gb]})
    out.sort(key=lambda g: -g["cost_r"])
    return [g for g in out if g["cost_r"] > 0]


# ---------------------------------------------------------------- the one proposal

def _json_line(key, value):
    return '  "%s": %s' % (key, json.dumps(value))


def make_proposal(gap, cfg, n_closed, verdict):
    """At most one proposal, as a diff. Never applied here, never touches the journal block."""
    if not gap:
        return None
    hyp = n_closed < HYPOTHESIS_BELOW
    p = {"gap": gap["kind"], "hypothesis": hyp or gap["kind"] in ("feature", "giveback"), "path": None,
         "diff": None, "text": None}
    at_cfg = cfg.get("autotrader") or {}
    if gap["kind"] == "symbol":
        old = list(cfg.get("blocked") or [])
        new = old + [gap["key"]]
        p["path"] = "blocked"
        p["diff"] = "-" + _json_line("blocked", old)[1:] + "\n+" + _json_line("blocked", new)[1:]
        p["text"] = "убрать %s из торговли (список blocked в trader/config.json)" % gap["key"]
    elif gap["kind"] == "time":
        lo, hi = gap["key"]
        first, last = TIME_BUCKETS[0], TIME_BUCKETS[-1]
        if (lo, hi) == last:
            k, old = "entry_until", at_cfg.get("entry_until", "10:30")
            new = _minus_minute(lo)
        elif (lo, hi) == first:
            k, old = "entry_from", at_cfg.get("entry_from", "09:50")
            new = TIME_BUCKETS[1][0]
        else:
            k, old, new = None, None, None
        if k:
            p["path"] = "autotrader." + k
            p["diff"] = '  "autotrader": {\n-' + _json_line(k, old)[1:] + "\n+" + _json_line(k, new)[1:] + "\n  }"
            p["text"] = "не входить в %s (autotrader.%s: %s → %s)" % (gap["label"], k, old, new)
        else:
            p["hypothesis"] = True
            p["text"] = ("окно %s в середине сессии входов: конфигом его не вырезать; проверить в backtest.py, "
                         "есть ли эффект на истории" % gap["label"])
    elif gap["kind"] == "feature":
        f, half, med = gap["key"]
        keep = ">=" if half == "low" else "<"
        p["text"] = ("фильтр входа: брать пробой только при %s %s %s. Ключа в конфиге нет, это изменение кода "
                     "autotrader.py и плана; сначала прогнать в backtest.py" % (f, keep, med))
        if f == "bar_rel_volume" and half == "low":
            p["text"] += " (ближайший существующий ключ: autotrader.min_bar_volume, но он в штуках, не в разах)"
    elif gap["kind"] == "giveback":
        p["text"] = ("стоп в безубыток после +%gR. autotrader стоп не двигает никогда, это изменение кода и "
                     "плана; сначала прогнать в backtest.py с тем же правилом" % GIVEBACK_MFE_R)
    if p["path"] and p["path"].split(".")[0] == "journal":
        raise ReviewError("a proposal may never touch the journal block")
    if verdict == "STOP":
        p["text"] += ". Вердикт STOP: правило в любом случае меняется письменно, и счёт paper начинается с нуля"
    return p


def _minus_minute(hm):
    h, m = int(hm[:2]), int(hm[3:])
    t = h * 60 + m - 1
    return "%02d:%02d" % (t // 60, t % 60)


# ---------------------------------------------------------------- the note

def _fmt(x, pct=False, money=False):
    if x is None:
        return "—"
    if pct:
        return "%.0f%%" % (100 * x)
    if money:
        return ("−$%.2f" if x < 0 else "$%.2f") % abs(x)
    if isinstance(x, float):
        return ("%.3f" % x).rstrip("0").rstrip(".") if abs(x) < 100 else "%.2f" % x
    return str(x)


def _delta(row):
    d = row["delta"]
    if d is None:
        return ""
    if d == 0:
        return " (=)"
    if row["key"] == "win_rate":
        return " (%+.0f п.п.)" % (100 * d)
    return " (%+g)" % round(d, 3)


def size_warning(gap):
    n, nt = gap["n"], gap["n_total"]
    days = " за %d торг. дн." % gap["days"] if gap.get("days") else ""
    clustered = (" Сделки одного утра коррелированы, так что независимых наблюдений ближе к числу дней, чем к n."
                 if gap.get("days") and gap["days"] < n else "")
    if nt < HYPOTHESIS_BELOW or n < 10:
        return ("Выборка мала (n = %d%s в группе из %d, t = %s): это шум, пока не доказано обратное. "
                "Ничего не меняем, смотрим, повторится ли.%s" % (n, days, nt, _na(gap.get("t")), clustered))
    if gap.get("t") is not None and abs(gap["t"]) < 2:
        return "t = %s: отличие от остальных сделок неотличимо от нуля." % gap["t"]
    return "n = %d из %d; t = %s." % (n, nt, _na(gap.get("t")))


def _na(x):
    return "—" if x is None else x


def render_note(date, rep, d, gaps, prop, trades):
    s = rep["stats"]
    today = [t for t in trades if t["date"] == date]
    lines = ["# Разбор %s" % date, ""]
    lines.append("**%d закрытых сделок · ожидание %s R · t = %s · PF %s · вердикт %s**" % (
        s["trades"], _fmt(s["expectancy_r"]), _fmt(s["t_r"]), _fmt(s["profit_factor"]), rep["verdict"]))
    lines.append("")
    if today:
        rs = [t["r"] for t in today if t["r"] is not None]
        lines.append("Сегодня: %d сделок, %s R, %s." % (len(today), _fmt(round(sum(rs), 3)) if rs else "—",
                                                       _fmt(round(sum(t["pnl"] for t in today), 2), money=True)))
        lines.append("")
        lines.append("| символ | вход | R | $ | выход | MFE, R | MAE, R |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for t in today:
            ex = t.get("excursion") or {}
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                t["symbol"], t.get("entry_hm") or "—", _fmt(t["r"]), _fmt(t["pnl"], money=True),
                t.get("exit_kind") or "—", _fmt(ex.get("mfe_r")), _fmt(ex.get("mae_r"))))
    else:
        lines.append("Сегодня закрытых сделок нет.")
    if rep["still_open"]:
        lines.append("")
        lines.append("Открыто (не считается): " + ", ".join("%s ×%g" % kv for kv in rep["still_open"].items()))
    lines += ["", "## Против прошлого разбора", ""]
    if d["verdict_prev"] is None:
        lines.append("Прошлого разбора нет: это первый, сравнивать не с чем.")
    else:
        lines.append("| показатель | было | стало |")
        lines.append("| --- | --- | --- |")
        for row in d["rows"]:
            pct = row["key"] == "win_rate"
            money = row["key"] == "max_drawdown"
            lines.append("| %s | %s | %s%s |" % (row["ru"], _fmt(row["prev"], pct, money), _fmt(row["cur"], pct, money),
                                                 _delta(row)))
        lines.append("")
        if d["verdict_changed"]:
            lines.append("**Вердикт изменился: %s → %s.**" % (d["verdict_prev"], d["verdict_cur"]))
        else:
            lines.append("Вердикт тот же: %s." % d["verdict_cur"])
    lines.append("")
    lines.append("Причины вердикта (journal.py, дословно):")
    for r in rep["reasons"]:
        lines.append("- " + r)
    lines += ["", "## Главный разрыв", ""]
    have_f = sum(1 for t in trades if t.get("features"))
    have_x = sum(1 for t in trades if t.get("excursion"))
    lines.append("Данные: %d сделок с R, %d с признаками входа, %d с MFE/MAE (логируются с 2026-11-02 16:29)." % (
        sum(1 for t in trades if t["r"] is not None), have_f, have_x))
    lines.append("")
    if not gaps:
        lines.append("Разрыва нет: ни одна группа (символ, время входа, половина признака, отдача прибыли) "
                     "не стоила больше остальных при n ≥ %d в группе. Это не новость, а отсутствие данных." % MIN_GROUP)
    else:
        g = gaps[0]
        if g["kind"] == "giveback":
            lines.append("**%s: %d из %d сделок, средний MFE %s R, закрылись в среднем на %s R. Стоили %s R.**" % (
                g["label"], g["n"], g["n_total"], _fmt(g["mean_mfe_r"]), _fmt(g["mean_r"]), _fmt(g["cost_r"])))
            lines.append("")
            lines.append("Сделки: " + ", ".join(g["symbols"]) + ".")
        else:
            what = {"symbol": "символ", "time": "время входа", "feature": "признак"}[g["kind"]]
            lab = g["label"]
            if g["kind"] == "feature":
                lab = "%s (%s)" % (g["label"], FEATURE_RU.get(g["key"][0], ""))
            lines.append("**%s %s: %d сделок, %s R в среднем против %s R у остальных %d. Стоило %s R.**" % (
                what, lab, g["n"], _fmt(g["mean_r"]), _fmt(g["rest_mean_r"]), g["n_rest"], _fmt(g["cost_r"])))
            if g["kind"] == "feature":
                lines.append("")
                lines.append("Признаков проверено до %d, по две половины: при стольких сравнениях один «разрыв» "
                             "с |t| около 2 находится случайно." % len(FEATURES))
        lines.append("")
        lines.append(size_warning(g))
        if len(gaps) > 1:
            lines.append("")
            lines.append("Следующие по величине: " + "; ".join(
                "%s %s (%s R, n = %d)" % (x["kind"], x["label"], _fmt(x["cost_r"]), x["n"]) for x in gaps[1:4]) + ".")
    lines += ["", "## Предложение", ""]
    if not prop:
        lines.append("Нет. Нечего предлагать без разрыва.")
    else:
        tag = "ГИПОТЕЗА, проверить в backtest.py" if prop["hypothesis"] else "ПРЕДЛОЖЕНИЕ"
        lines.append("**%s (не применено, решает Сергей письменно):** %s." % (tag, prop["text"]))
        if prop["diff"]:
            lines += ["", "```diff", "  // trader/config.json", prop["diff"], "```"]
        lines.append("")
        if s["trades"] < HYPOTHESIS_BELOW:
            lines.append("Меньше %d сделок: это гипотеза, а не изменение. Честный путь: выбрать вариант на "
                         "августе–сентябре в backtest.py и проверить на октябре (вне выборки)." % HYPOTHESIS_BELOW)
        lines.append("Любое изменение правила обнуляет счёт paper-сделок. Планки в блоке journal не трогаются.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def _read(path, what, required=True):
    try:
        return J.read_json(path, what, required)
    except J.JournalError as e:
        raise ReviewError(str(e))


def ny_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def review(date, state, cfg, journal_doc, prev):
    rep = build_report(state, cfg, journal_doc)
    trades = enrich(rep["trades"], state)
    d = diff(rep, prev)
    gaps = find_gaps(trades)
    prop = make_proposal(gaps[0] if gaps else None, cfg, rep["stats"]["trades"], rep["verdict"])
    note = render_note(date, rep, d, gaps, prop, trades)
    snap = {k: v for k, v in rep.items() if k != "trades"}
    snap.update({"date": date, "gap": gaps[0] if gaps else None, "proposal": prop,
                 "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return note, snap


def cmd_run(a):
    date = a.date or ny_today()
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ReviewError("--date wants YYYY-MM-DD")
    state = _read(a.state, "broker log")
    cfg_path = a.config if os.path.exists(a.config) else a.config_fallback
    cfg = _read(cfg_path, "config", required=False) or {}
    jdoc = _read(a.journal, "journal", required=False)
    prev = previous_snapshot(a.reviews, date)
    note, snap = review(date, state, cfg, jdoc, prev)
    os.makedirs(a.reviews, exist_ok=True)
    md = os.path.join(a.reviews, date + ".md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(note)
    J.write_json(os.path.join(a.reviews, date + ".json"), snap)
    if a.print:
        print(note)
    print("review: %s (%d trades, %s, gap: %s)" % (md, snap["stats"]["trades"], snap["verdict"],
                                                  snap["gap"]["kind"] if snap["gap"] else "none"))
    return 0


def pull_commands(host, repo, out_dir):
    """The two read-only copies, as argv lists. scp only reads the remote; nothing is written there."""
    cmds = []
    for rel, name in (("trader/state/broker.json", "broker.json"), ("trader/config.json", "config.json")):
        cmds.append(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                     "%s:%s/%s" % (host, repo, rel), os.path.join(out_dir, name + ".part")])
    return cmds


def cmd_pull(a):
    os.makedirs(a.out, exist_ok=True)
    for cmd in pull_commands(a.host, a.repo, a.out):
        part = cmd[-1]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise ReviewError("scp failed (%d): %s" % (r.returncode, r.stderr.strip()))
        with open(part, encoding="utf-8") as fh:
            try:
                json.load(fh)
            except ValueError as e:
                raise ReviewError("%s is not valid JSON: %s" % (part, e))
        os.replace(part, part[:-5])
        print("pulled", part[:-5])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nightly review: one gap, at most one proposal, never applied.")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="write trader/reviews/DATE.md and DATE.json")
    r.add_argument("--date", help="YYYY-MM-DD, default today in New York")
    r.add_argument("--state", default="trader/vps/broker.json", help="pulled copy of the VPS broker log")
    r.add_argument("--config", default="trader/vps/config.json")
    r.add_argument("--config-fallback", default="trader/config.json")
    r.add_argument("--journal", default="trader/journal.json")
    r.add_argument("--reviews", default="trader/reviews")
    r.add_argument("--print", action="store_true", help="also print the note")
    p = sub.add_parser("pull", help="read-only scp of broker.json and config.json from the VPS")
    p.add_argument("--host", default=VPS_HOST)
    p.add_argument("--repo", default=VPS_REPO)
    p.add_argument("--out", default="trader/vps")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"run": cmd_run, "pull": cmd_pull}[a.cmd](a)
    except ReviewError as e:
        print("review: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
