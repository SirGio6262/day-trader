# day-trader

A day-trading workstation for Claude Code: the **Day Trader skill pack** (the
Trader skill v2 by Jake Schincariol, eleven skills, MIT) installed as
project-local skills, plus a twelfth skill of our own, `trader-journal`,
which measures whether the trading actually makes money and gates the move
from paper to live.

Upstream: https://github.com/Jakeschincariol/day-trader-skill (see `LICENSE`).
Installed from upstream commit `3d0f4ef` (2026-10-20, v2.0.0).

## Skills

| command | job |
| --- | --- |
| `/trader-scanner` | Chart Scanner: opening-range breakouts, flags, oversold bounces |
| `/trader-news` | News Monitor: Yahoo headlines and SEC 8-Ks per ticker |
| `/trader-entry` | Entry Timer: watches a setup bar by bar |
| `/trader-exit` | Exit Planner: stop and target before entry |
| `/trader-timemachine` | Time Machine: 10,000 replays plus every past firing of the setup |
| `/trader-budget` | Budget Manager: sizes the position, writes one bracket order |
| `/trader-risk` | Risk Manager: PASS or BLOCK against your limits |
| `/trader-broker` | Order Executor: paper by default, never without your yes |
| `/trader-alerts` | Price Alerts: stop, target, flat by close, opening bell |
| `/trader-analyst` | Earnings Analyst: XBRL facts from SEC EDGAR |
| `/trader-pelosi` | Pelosi Tracker: House PTR disclosures |
| `/trader-journal` | **Trade Journal and go-live gate** (ours): expectancy, t-stat, drawdown, verdict |

One trade:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker -> (fill) -> journal
```

The daily run, the hard rules and the stage plan are in `CLAUDE.md`, which
Claude Code loads on every session in this folder.

## Setup (one evening, on your own machine)

The data sources (Yahoo Finance, Stooq, Nasdaq Trader, SEC EDGAR, the House
Clerk) are blocked from Claude's cloud sandbox, so this runs locally.

1. Python 3.10+ and `uv` (https://docs.astral.sh/uv/). No pip packages.
   `pdftotext` (poppler) only for `/trader-pelosi --read`.
2. Clone this repo, then the config:

   ```bash
   mkdir -p trader && cp .claude/skills/trader-budget/config.example.json trader/config.json
   ```

   Edit `trader/config.json`: your `budget`, `max_loss_per_trade_usd`,
   `max_daily_loss_usd`, `max_trades_per_day`, `sec_user_agent` (your name
   and email, the SEC requires it), and add the `journal` block from
   `.claude/skills/trader-journal/SKILL.md`. Keep `"paper": true`.
3. An Alpaca account with **paper** API keys (https://alpaca.markets). Put
   them in your shell, never in a file in this repo:

   ```bash
   export ALPACA_API_KEY=... ALPACA_SECRET_KEY=... ALPACA_PAPER_TRADE=true
   ```

   `.mcp.json` hands them to Alpaca's official MCP server
   (`uvx alpaca-mcp-server`); copy `claude-settings.example.json` to `.claude/settings.json`, which keeps every order tool
   on "ask" and the batch, options and crypto tools denied.
4. `claude` in this folder, approve the project MCP server when asked, then
   `/mcp` to see it connected, then ask for the Alpaca account info and check
   in the Alpaca dashboard that it is the paper account.
5. `python3 -m unittest discover -s tests` should pass.

## Как этим зарабатывать (и когда это невозможно)

Честно: сам по себе набор скиллов не даёт преимущества. Он даёт дисциплину
(стоп и цель до входа, размер позиции от допустимого убытка, запрет на
сделку вне правил) и честную проверку (Time Machine). Приносит ли деньги
конкретный сетап в ваших руках, решает только ваш собственный журнал сделок.
Поэтому порядок такой:

1. **Бумажная торговля, минимум 60 закрытых сделок** по одному сетапу
   (opening-range breakout), по ежедневному регламенту из `CLAUDE.md`, с
   записью каждого исполнения (`ticket.py fill`). Два-четыре месяца при
   одной-трёх сделках в день.
2. **Каждый вечер `/trader-journal report`.** Он считает ожидание в R,
   стандартную ошибку, t-статистику, profit factor, просадку, и даёт один
   вердикт: NOT YET, GO или STOP. STOP при t ≤ −2 после 30 сделок означает,
   что сетап убыточен, и больше сделок это не исправит: меняйте правило,
   счёт сначала.
3. **GO означает одно**: живой счёт с тем же долларовым риском на сделку,
   ещё 60 сделок, затем `report --live` и сравнение с бумагой. Не больше
   размера, не больше сделок в день, не второй сетап.
4. **Масштаб только по правилу**: единственные рычаги — `max_loss_per_trade_usd`
   и `max_daily_loss_usd` в конфиге, и двигаются они после прохождения
   ворот на живом счёте, а не после удачной недели.

Чего ждать от данных: котировки Yahoo бесплатные, неофициальные и с
задержкой, при частых запросах блокируются на время. Вход только по закрытию
бара, который назвал скилл, или никакого входа.

## Updating from upstream

```bash
git clone --depth 1 https://github.com/Jakeschincariol/day-trader-skill.git /tmp/dts
rm -rf .claude/skills/trader-{alerts,analyst,broker,budget,entry,exit,news,pelosi,risk,scanner,timemachine}
cp -r /tmp/dts/trader-* .claude/skills/
cp /tmp/dts/LICENSE LICENSE
```

`trader-journal` is ours and is not in upstream; the loop above leaves it alone.
