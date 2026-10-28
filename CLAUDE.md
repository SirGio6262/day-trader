# day-trader: operating manual for Claude

This project is a day-trading workstation built from the Day Trader skill
pack (eleven upstream skills in `.claude/skills/trader-*`) plus one skill of
our own, `trader-journal`, which is the go-live gate. The owner is Sergey, a
physicist. Speak to him in the language he writes in, and lead with numbers.

## The one rule that outranks every other

Nothing in this project has an edge by itself. The scanner finds charts that
match a rule, the time machine shows the odds of one plan, the risk manager
keeps a loss small. Whether the rule makes money is an empirical question
that only `trader-journal` can answer, from Sergey's own closed trades. So:

- Real money is never discussed as a next step unless `journal.py report`
  says GO on its current record. Quote the verdict and the reasons.
- A STOP verdict ends trading of that setup, paper or live, until the rule
  is changed in writing in `trader/config.json` or the plan, and the paper
  count starts again from zero.
- Never lower a bar in the `journal` block of the config to make a verdict
  pass. Sergey changes the gate, on purpose, and says so.

## Hard rules on orders (from the pack, kept here so they are never lost)

- No order is placed without Sergey's explicit yes to that one ticket, in
  the chat, after `ticket.py show`. "Yes to all", "ok", "sure" do not count.
- `trader-risk` runs before every `trader-broker` call, every time. BLOCK is
  final for that order.
- Paper is the default (`"paper": true` in the config and
  `ALPACA_PAPER_TRADE=true`). Switching to live is a config change Sergey
  makes by hand, never a chat instruction Claude carries out.
- Long only, whole shares, bracket orders (entry, stop-loss, take-profit),
  flat by `flat_by` (15:55 New York) whatever the position shows.
- API keys never appear in the chat, in a file in this repo, or in a tool
  call. They live in the shell environment (`.mcp.json` reads them from
  there) or in Claude Desktop's config.
- After every fill, in both directions, record it: `ticket.py fill`. The
  journal is blind otherwise.

## The daily run (New York time; the skills work out the clock themselves)

| when | what | skill |
| --- | --- | --- |
| before 09:00 | universe once a week, daily scan, news on the hits | `/trader-scanner universe` (weekly), `/trader-scanner daily`, `/trader-news` |
| 09:30 | nothing. The opening range is forming. | |
| 09:50 to 10:30 | opening-range breakouts, then watch the best one bar by bar | `/trader-scanner orb`, `/trader-entry` |
| on a trigger | stop and target first, then the odds, then the size, then the check, then the ticket | `/trader-exit`, `/trader-timemachine`, `/trader-budget`, `/trader-risk`, `/trader-broker` |
| while open | stop, target and flat-by alerts | `/trader-alerts` |
| 15:55 | flatten anything still open | `/trader-broker` (flatten), `/trader-risk`, yes, `ticket.py fill` |
| after the close | the journal, every day | `/trader-journal report` |
| weekly | CSV export, by-symbol and by-setup review | `/trader-journal csv`, `report --json` |

No entries before `no_entries_before` (09:45) or after `no_entries_after`
(15:30). Earnings this week for the symbol: a day trade is fine, holding
through the report is not, and `/trader-analyst` says when it reports.

Skip a time-machine verdict of "coin flip" or worse. That is the whole point
of running it before the budget step.

## The stages

1. **Setup** (one evening): Python 3.10+, `uv`, an Alpaca paper account,
   `trader/config.json` from the example with Sergey's own budget and
   limits, `.mcp.json` with the keys in the environment, one read-only
   Alpaca call to prove it is the paper account.
2. **Paper, 60 closed trades minimum**: the daily run exactly as above, one
   setup, every fill recorded. The journal decides when this stage ends.
   Expect it to take two to four months at one to three trades a day.
3. **Live, small**: the same dollar risk per trade as paper, 60 more trades,
   then `report --live`. Size steps up by one notch only on a live GO.
4. **Scale by rule**: the config's `max_loss_per_trade_usd` and
   `max_daily_loss_usd` are the only levers, and they move after a gate
   pass, never after a good week.

## Files

- `trader/config.json` (gitignored): budget, limits, the `journal` gate,
  `sec_user_agent`. Start from `.claude/skills/trader-budget/config.example.json`
  and add the `journal` block from `.claude/skills/trader-journal/SKILL.md`.
- `trader/` (gitignored): everything the skills write. `trader/state/` is
  append-only logs. Never delete `trader/state/broker.json`: it is the record.
- `.mcp.json`: the Alpaca MCP server for Claude Code, keys from the environment.
- `claude-settings.example.json`: copy it to `.claude/settings.json` (gitignored). Order tools on ask, batch and option tools denied.
- `tests/`: `python3 -m unittest discover -s tests` must pass before a push.

## Data sources and their limits

Free and unofficial: Yahoo Finance chart JSON (delayed, rate limited, can
block an IP for a while), Stooq, Nasdaq Trader symbol files, SEC EDGAR (needs
`sec_user_agent`), the House Clerk. When Yahoo answers 429, stop scanning for
ten minutes rather than retrying; use `--offline` fixtures to keep working.
A delayed quote is a reason to never chase: the entry is the bar close the
skill names, or nothing.

This repo's cloud environment cannot reach any of these hosts unless its
network policy allows them. The daily run happens on Sergey's own machine.
