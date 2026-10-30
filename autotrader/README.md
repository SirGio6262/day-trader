# autotrader: the unattended executor

One Python process that trades the pack's opening-range breakout rule on a
fixed list of liquid symbols through Alpaca, with the stop and the target
held at the broker inside a bracket order, flat by 15:55, and every decision
and fill written to `trader/state/broker.json` in trader-broker's own shape,
so `/trader-journal` reads it unchanged. Paper unless `trader/config.json`
and `ALPACA_PAPER_TRADE` both say live.

What it does, in order, every trading day (New York time):

1. 09:31 start. Any position it did not open today (a flatten that failed,
   a buy by hand) is closed at market first, at the open. The first 15
   minutes (three 5-minute bars) are the range.
2. 09:50 to 10:30: every poll (15 s) it fetches bars for the symbols and
   takes the first completed bar that closes above the range high. A bar
   that completed more than 5 minutes ago is never chased: only a later
   bar that also closes above the high counts. Strongest breakout first
   when several fire in the same bar.
3. Stop at the range midpoint, target at `target_r` times the risk, whole
   shares sized by the smallest of the config's limits, then the risk
   check (per-trade loss, daily loss, trades per day, window, blocked
   list, reward to risk). PASS sends one bracket market order.
4. Polls fills; logs the entry fill and whichever leg (stop or target)
   fills later.
5. 15:55: cancels the legs of every open trade, waits (up to 30 s, for
   all symbols at once) until Alpaca frees the shares they hold, sends one
   market close per symbol (re-sent for up to 30 s more while Alpaca still
   answers "insufficient qty"), and waits up to 60 s for the fills. The
   whole flatten takes about two minutes at worst, however many positions
   are open. If a symbol still cannot be closed, Alpaca's close-all
   (cancel every order, liquidate everything) is the last resort; a
   partially filled entry is closed for what filled.
6. Prints the journal report and sends it to Telegram if configured.

`trader/STOP` (any content) stops new entries and flattens at once. Delete
the file to allow trading again.

What it never does: short, options, crypto, add to a position, move a
stop, a second trade in the same symbol the same day, anything when the
config says `allow_short`, `allow_options`, `allow_crypto` or disables
`require_stop`.

## Config

Everything comes from `trader/config.json` (the pack's limits) plus an
optional `autotrader` block:

```json
"autotrader": {
  "symbols": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO", "NFLX",
              "JPM", "BAC", "XOM", "COST", "WMT", "HD", "LLY", "V", "MA", "CRM"],
  "or_minutes": 15,
  "entry_from": "09:50",
  "entry_until": "10:30",
  "target_r": 2.0,
  "min_bar_volume": 0,
  "min_stop_distance_pct": 0.15,
  "poll_seconds": 15,
  "feed": "iex",
  "use_scanner_watchlist": false
}
```

`feed`: `iex` is free (Alpaca's Basic plan, real time from the IEX
exchange only, 200 calls a minute). `sip` is the full tape and needs the
$99 a month Algo Trader Plus plan; consider it only after the journal gate.
`use_scanner_watchlist: true` reads `trader/watchlist.json` from
`/trader-scanner daily` if you run the scanner pre-market by cron.

## VPS setup (Ubuntu 24.04, any $5-10 a month box)

```bash
sudo adduser --disabled-password --gecos "" trader
sudo -iu trader
git clone https://github.com/SirGio6262/day-trader.git
cd day-trader
python3 -m venv venv && venv/bin/pip install -r autotrader/requirements.txt
mkdir -p trader && cp .claude/skills/trader-budget/config.example.json trader/config.json
```

Edit `trader/config.json` (budget, limits, `sec_user_agent`, the
`journal` and `autotrader` blocks). Then the keys, in a file only root and
`trader` can read:

```bash
cat > /home/trader/day-trader.env <<'EOF'
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER_TRADE=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
EOF
chmod 600 /home/trader/day-trader.env
```

Check everything once, by hand:

```bash
set -a; . /home/trader/day-trader.env; set +a
venv/bin/python autotrader/autotrader.py check
```

It prints the config, the account (confirm it says paper and the cash is
the paper $100,000), the clock and a bar request. Then the service:

```bash
sudo cp /home/trader/day-trader/autotrader/day-trader.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now day-trader
sudo systemctl status day-trader
```

The daemon sleeps until 09:45 on each trading day (it asks Alpaca's clock,
so holidays are handled), runs the session, and sleeps again. Logs:
`trader/state/autotrader.log` (every decision) and
`trader/state/autotrader.out` (the process). Telegram: a message at the
start, at every order, every fill, the flatten, and the journal report.

Telegram in two minutes: talk to `@BotFather`, `/newbot`, copy the token;
send your bot any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.

## Daily commands

```bash
venv/bin/python autotrader/autotrader.py status     # positions, open orders, today's state
venv/bin/python autotrader/autotrader.py flatten    # close everything now
touch trader/STOP                                   # no new entries, flatten at once; rm it to resume
venv/bin/python .claude/skills/trader-journal/journal.py report
```

## Going live, later

Only on a GO from the journal (60 closed paper trades through this exact
process). Then: live keys in the env file, `ALPACA_PAPER_TRADE=false`,
`"paper": false` in the config, the same `max_loss_per_trade_usd`, restart
the service, and 60 more trades before `report --live` says anything.

## Known limits

- IEX-only bars can differ from the consolidated tape; a breakout on IEX
  is not always a breakout on SIP. Paper trading will show how often.
- Market entries fill at the next print, not the bar close the rule saw.
  The journal measures the real fill, so slippage shows in R.
- Partial fills: the process waits for `filled` before it reports an
  entry; at 15:55 a partially filled entry is canceled and the filled
  shares are closed, and a stop or target that filled in part is logged
  as its own sell so the journal stays whole.
- One process, one account. Do not run two copies against the same keys.
