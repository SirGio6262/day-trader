---
name: trader-journal
description: >-
  Trade Journal and the go-live gate. Reads every fill trader-broker recorded,
  pairs each buy with its sell, measures every closed trade in R (profit over
  the risk at the stop) and in dollars net of costs, and prints the sample
  size, win rate, expectancy, standard error, t-statistic, profit factor,
  drawdown and worst losing streak, by symbol and by setup. Then one verdict
  against the gate in trader/config.json: NOT YET (keep paper trading), GO
  (live, small, same risk) or STOP (the setup loses; change it). Records
  trades made outside the pack and exports a CSV. Use when the user says "how
  am I doing", "am I profitable", "show my stats", "what is my expectancy",
  "can I go live", "should I stop", "log this trade", "export my trades",
  "journal", at the end of every trading day, and before any talk of real
  money.
argument-hint: "[report | list | add | csv] [--live | --all]"
---

# trader-journal

The other ten skills find, time, size, check and place one trade at a time.
None of them answers the only question that decides whether you keep any
money: does this setup, traded your way, make money after costs? This skill
does, from your own record, and it is the only thing in the pack allowed to
say the words "go live".

`${CLAUDE_SKILL_DIR}` below is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/journal.py" report              # paper trades, the default
python3 "${CLAUDE_SKILL_DIR}/journal.py" report --live       # live trades only
python3 "${CLAUDE_SKILL_DIR}/journal.py" report --all --json
python3 "${CLAUDE_SKILL_DIR}/journal.py" list
python3 "${CLAUDE_SKILL_DIR}/journal.py" add --symbol TSLA --date 2026-10-19 --qty 8 \
    --entry 364.18 --exit 358.32 --stop 358.32 --setup orb --note "stopped out"
python3 "${CLAUDE_SKILL_DIR}/journal.py" csv trader/journal.csv
```

## Where the trades come from

- `trader/state/broker.json`, which trader-broker appends to: `filled` rows
  (from `ticket.py fill`) are paired per symbol, oldest buy to oldest sell.
  The stop of each trade is the stop of the last buy you approved for that
  symbol (the `approved` or `placed` row), so **record every fill with
  `ticket.py fill`, both the entry and the exit**, or the journal is blind.
- `trader/journal.json`, trades you add here with `add` (another broker, a
  trade by hand). `--stop` is optional but without it the trade has no R.

Every number is net of `journal.cost_per_share_round_trip` from the config
(default $0.02 a share, the spread plus slippage on a commission-free
broker). Change it if your fills say otherwise; never set it to zero.

## The statistics

For N closed trades: P&L total and per trade, wins, losses, win rate,
average win and loss, profit factor (gross wins over gross losses),
expectancy in R with its standard deviation, standard error, t-statistic
(mean R over its standard error) and 95 % confidence interval, average
winning and losing R, maximum drawdown in dollars and as a percent of the
budget, and the worst losing streak. Then the same per symbol and per setup.

Read them like this. The t-statistic says whether the mean is
distinguishable from zero given the scatter: at t = 1 a mean of +0.2 R is
noise, at t = 2.5 it is probably real. The confidence interval says how
wrong the mean may still be. The profit factor says what the dollar edge
looks like after the costs. The drawdown says what it felt like to live
through it, and what a live run should expect to feel.

## The gate (the `journal` block in trader/config.json)

```json
"journal": {
  "min_trades": 60,
  "min_expectancy_r": 0.10,
  "min_t": 2.0,
  "min_profit_factor": 1.3,
  "max_drawdown_pct": 10,
  "stop_trades": 30,
  "stop_t": -2.0,
  "cost_per_share_round_trip": 0.02
}
```

The verdict, in order:

1. **STOP** when at least `stop_trades` are closed and t is at or below
   `stop_t`: the setup loses, and more trades will not fix it. Change the
   rule (the setup, the stop, the market, the time of day), then start the
   paper count again from zero.
2. **NOT YET** while fewer than `min_trades` are closed, whatever the
   numbers say. 20 good trades are a good month, not an edge.
3. With a full sample, **GO** only when every bar clears: mean R at least
   `min_expectancy_r`, t at least `min_t`, profit factor at least
   `min_profit_factor`, drawdown within `max_drawdown_pct` of the budget.
   Any bar missed is NOT YET, with the reason.

GO means one thing: trade live at the **same dollar risk per trade** as the
paper run for the next `min_trades` trades, then `report --live` and
compare the two. It never means more size, more trades a day, or a second
setup. Size goes up by one step only when the live sample passes the same
gate; it comes down the day a live drawdown passes the bar.

## How to run it

1. At the end of every trading day, after the flatten: `report`. Read the
   user the verdict and the one line that changed (a new worst streak, a
   drawdown step, the t-statistic crossing 1, 1.5, 2).
2. Weekly: `csv`, so the user can look at the trades in a spreadsheet, and
   `report --json` by symbol and setup. A setup that loses on one symbol
   and wins on another is two setups.
3. Before any talk of live money, run `report` and quote the verdict. If
   it is not GO, the answer to "should I go live" is no, with the numbers.
4. If fills are missing (a sell without a buy, a position still open at
   night), say so: the journal lists what is still open.

## What it never does

- Never counts a trade that is not closed, and never guesses an exit.
- Never says GO from paper numbers alone past the first live block: the
  live sample has to pass on its own.
- Never lowers a bar in the gate because the numbers are close. The bars
  change only when the user changes the config, on purpose, in writing.
- Never places, changes or cancels an order. It reads the record.
