# day-trader

Project workspace for the **Day Trader skill pack** (the Trader skill v2) by
Jake Schincariol: eleven Claude Code skills for day trading, installed as
project-local skills in `.claude/skills/`.

Upstream: https://github.com/Jakeschincariol/day-trader-skill (MIT, see `LICENSE`).
Installed from upstream commit `3d0f4ef` (2026-10-20, v2.0.0).

## Skills

| command | job |
| --- | --- |
| `/trader-scanner` | Chart Scanner: opening-range breakouts, flags, oversold bounces |
| `/trader-news` | News Monitor: Yahoo headlines and SEC 8-Ks per ticker |
| `/trader-entry` | Entry Timer: watches a setup bar by bar |
| `/trader-exit` | Exit Planner: stop and target before entry |
| `/trader-timemachine` | Time Machine: 10,000 replays plus every past firing of the setup |
| `/trader-risk` | Risk Manager: PASS or BLOCK against your limits |
| `/trader-budget` | Budget Manager: sizes the position, writes one bracket order |
| `/trader-broker` | Order Executor: paper by default, never without your yes |
| `/trader-alerts` | Price Alerts: stop, target, flat by close, opening bell |
| `/trader-analyst` | Earnings Analyst: XBRL facts from SEC EDGAR |
| `/trader-pelosi` | Pelosi Tracker: House PTR disclosures |

Typical flow for one trade:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker
```

## Requirements

- Python 3.8 or newer. No pip dependencies.
- The SEC tools (`/trader-news`, `/trader-analyst`) need a user agent: set
  `SEC_USER_AGENT="Your Name you@example.com"` in the environment, or put
  `"sec_user_agent"` in `trader/config.json`.
- `/trader-pelosi --read` needs `pdftotext` (poppler) on the PATH.
- `/trader-broker` places orders only through a brokerage MCP connector
  (Alpaca paper account by default); see `.claude/skills/trader-broker/SKILL.md`.

Everything the skills produce lands in `trader/`, which is gitignored: it
holds your watchlist, plans, orders and config.

## Updating from upstream

```bash
git clone --depth 1 https://github.com/Jakeschincariol/day-trader-skill.git /tmp/dts
rm -rf .claude/skills/trader-*
cp -r /tmp/dts/trader-* .claude/skills/
cp /tmp/dts/LICENSE LICENSE
```
