# Multi-Agent Portfolio Analysis

Five specialized Claude agents read a portfolio snapshot and produce a
structured risk, thesis, and rebalancing report — no dashboards to babysit,
no manual spreadsheet triage. Point it at an xlsx, get back JSON + a
self-contained HTML report.

Built on the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview),
running Haiku 4.5 for the specialist agents and Sonnet 5 for synthesis.
Reference deployment: a Docker container on a Synology NAS, scheduled
weekly — but the core (`supervisor.py`) is just Python + pandas and runs
anywhere.

## Why this exists

Position-by-position portfolio review doesn't scale past a handful of
holdings, and generic robo-advisor tools don't reason about concentration,
correlated risk, or thesis quality — they just rebalance to a target
allocation. This system runs four lane-disciplined agents (no redundant
re-analysis, no agent re-deriving what another already established) in
parallel over a static snapshot, then a fifth synthesizes and recommends,
so you get an actual second opinion instead of a wall of numbers.

## What it does

- **Portfolio Structure Analyst** — concentration (HHI), sector/factor
  clustering, coverage gaps
- **Market & Fundamental Analyst** — investment thesis, quality, valuation,
  secular risk per top holding
- **Risk Analyst** — volatility, beta, correlation, stress scenarios, data
  quality flags
- **Chief Strategist** — synthesizes the three into one recommendation
  (Sonnet 5, runs after the others)
- **Rebalancing Analyst** — concrete trim/add/hold calls across the full
  book, not just the top risk contributors
- Local-only price/volatility CSV support with staleness detection — no
  live market-data dependency for the core analysis
- Self-contained tabbed HTML report (light/dark aware) alongside the raw
  JSON

## Quickstart (synthetic example data, no real portfolio needed)

```bash
git clone <this-repo>
cd multi-agent-portfolio-analysis
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
cp data/closing_prices.example.csv data/closing_prices.csv
cp data/volatility.example.csv data/volatility.csv

# Bring your own portfolio.xlsx (columns: ticker, name, weight_pct, sector,
# volatility, risk_contribution_pct, market_beta), or build a small test one
# from data/volatility.example.csv's tickers/weights.
python3 app/supervisor.py ./data/portfolio.xlsx example
```

Output: `reports/multi-agent-report_example.{json,html}`. Cost is roughly
$0.15–$0.40 per full run (5 agents; 4 on Haiku 4.5, the Chief Strategist on
Sonnet 5).

## Architecture

```
portfolio.xlsx  +  closing_prices.csv / volatility.csv
    |
    v
supervisor.py  (reads portfolio.xlsx directly, no API calls, no
    |            intermediate JSON; top_5_by_risk: dynamic coverage-based
    |            list, not fixed at 5; all_positions: full book, for
    |            rebalancing)  (+ PriceAnalyzer for drawdown/vol context)
  / | \
Portfolio   Market&Fundamental   Risk        <- run in parallel (haiku-4.5)
Analyst     Analyst              Analyst
  \ | /
Chief Strategist / meta-analyst  <- sequential, synthesizes the three (sonnet-5)
    |
    v
Rebalancing Analyst               <- haiku-4.5, full-book trim/add/hold calls
    |
    v
multi-agent-report_<name>.json + .html
```

Agent roles and prompts live in `app/agent_skills.md` — editable without
touching Python. Each agent has an explicit OWNS/DEFER boundary so no two
agents re-derive the same conclusion; shared facts (coverage, data-quality
flags) are injected once via `shared_givens` rather than re-computed per
agent.

## Configuration

```bash
cp config/env.example config/.env
```

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Claude Agent SDK auth |
| `ANTHROPIC_WORKSPACE_ID` | no | Workspace scoping, if your org requires it |
| `EODHD_API_KEY` | no | Reserved for the not-yet-integrated live price refresh (`eodhd_client.py`) |
| `PORTFOLIO_DATA` / `PORTFOLIO_REPORTS` | no | Override the `/data` and `/reports` mount points |

## Input format (`portfolio.xlsx`)

One row per position: `ticker`, `name`, `weight_pct`, `sector`, `volatility`,
`risk_contribution_pct`, `market_beta` (falls back to legacy
`beta_to_portfolio` / `relative_volatility` if present). See
`data/volatility.example.csv` for representative tickers/weights and
`data/README.md` for the full data-directory layout.

## Reference deployment: Synology NAS (Docker)

The system is deployment-agnostic — `supervisor.py` just needs Python,
pandas, and the Claude Agent SDK. The scripts below (`pull-to-nas.sh`,
`run_custom_portfolio.sh`) document one working reference deployment (DSM6,
host networking, weekly Task Scheduler run); adapt the paths for your own
environment.

### Deploy

Pull-on-NAS-as-root, not scp-push (avoids the SSH-user/home-folder ACL
problem that blocks the container from reading bind mounts):

```bash
ssh <nas-user>@<nas-host>
# first run: from your Drive-synced (or otherwise mirrored) working copy
sudo /path/to/your/synced/multi-agent/pull-to-nas.sh --build
# later:
sudo /volume1/docker/portfolio-multi-agent/pull-to-nas.sh --build
```

`pull-to-nas.sh` copies `app/*.py`, `app/*.md`, the Docker files and the
data/price CSVs into place, then `chown`s the project dir to the container
uid (1000). It never overwrites `config/.env`. Any change to `app/*.py` or
`app/*.md` (including `agent_skills.md`) requires `--build` to take effect —
those files are baked into the image, not bind-mounted.

### First-time config and build

```bash
cd /volume1/docker/portfolio-multi-agent
cp config/env.example config/.env
nano config/.env   # set ANTHROPIC_API_KEY (+ ANTHROPIC_WORKSPACE_ID if required)

sudo docker build --network=host -t portfolio-multi-agent:latest .

sudo docker run --rm \
  --network host \
  --env-file ./config/.env \
  -v ./data:/data:ro \
  -v ./reports:/reports \
  portfolio-multi-agent:latest
```

### Scheduled weekly run

A NAS-only `run_weekly_analysis.sh` (not in this repo — hand-maintained per
deployment) is expected to: sync fresh `portfolio.xlsx`/price CSVs, run the
container against `portfolio.xlsx`, copy the report into a synced folder,
and log the run. Wire this up via DSM Task Scheduler or cron.

### Custom / one-off portfolio runs

`run_custom_portfolio.sh` runs the analysis against an xlsx living anywhere,
under its own name, without touching the scheduled run's output. Edit the
three variables at the top before running:

```bash
SOURCE_DIR="/path/to/folder"      # where the xlsx currently lives
SOURCE_FILE="some_portfolio.xlsx"
PORTFOLIO_NAME="custom_name"       # names every output file — never "portfolio"
```

Output lands at `reports/multi-agent-report_<name>.{json,html}`.

## Files

- `app/supervisor.py` — Reads `portfolio.xlsx` directly (no intermediate
  JSON), orchestrates all 5 agents (Claude Agent SDK), writes the JSON +
  HTML report
- `app/price_analyzer.py` — Local price/volatility CSV analytics
  (volatility, drawdown, momentum, staleness check)
- `app/eodhd_client.py` — EODHD REST API wrapper (prepared, not yet
  integrated)
- `app/agent_skills.md` — Agent roles/prompts, editable without touching
  Python
- `Dockerfile` / `docker-compose.yml` — Python 3.11-slim, non-root user
- `pull-to-nas.sh` / `run_custom_portfolio.sh` — Reference NAS deployment
  scripts (see above)
- `data/` — Input: `portfolio.xlsx` (bring your own, gitignored) + price/
  volatility CSVs (`*.example.csv` are safe synthetic samples)
- `reports/` — Output: JSON + HTML analysis reports (generated per run, not
  committed)

## Known limitations

- Single hardcoded portfolio name (`portfolio`) in the reference *scheduled*
  pipeline — no automated multi-portfolio support (`run_custom_portfolio.sh`
  covers manual/occasional second-portfolio runs)
- Reports are overwritten in place each run — no built-in version history
- `top_5_by_risk` is not actually fixed at 5: it's every name needed to
  cover ≥50% of total portfolio risk (min 5) — field name kept for backward
  compatibility with existing consumers
- EODHD live price refresh not yet wired in
- Web-search Research Analyst (designed, not implemented)
- No automated test suite yet

## Notes

- **Network:** some DSM6 NAS setups require `--network=host` for containers
  to reach the internet.
- **Memory:** low-RAM NAS boxes may need `mem_limit`/`memswap_limit` tuning
  (see `docker-compose.yml`).
- **Python SDK:** `claude-agent-sdk` bundles the CLI; no Node.js needed.

## License

MIT — see `LICENSE`.
