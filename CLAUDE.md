# Multi-Agent Portfolio Analysis System — Architecture Overview

**Status:** 5 agents, offline-first, reference deployment on a scheduled Docker
container (see README for a generic quickstart and the reference NAS setup).

------------------------------------------------------------------------

## System Architecture

```
Input: portfolio.xlsx (aggregated risk view)
   ↓
supervisor.py (reads portfolio.xlsx directly, no API calls, no intermediate
                JSON file; async agent orchestrator)
   ├─ Portfolio Analyst            (haiku-4.5) → structure, concentration, gaps
   ├─ Market & Fundamental Analyst (haiku-4.5) → thesis, quality, valuation, secular risk
   └─ Risk Analyst                 (haiku-4.5) → volatility, correlation, stress, drawdown
        (the three run in parallel via asyncio.gather())
   ↓
Chief Strategist / meta-analyst  (sonnet-5) → synthesizes the three into a recommendation
   ↓
Rebalancing Analyst              (haiku-4.5) → full-book trim/add/hold calls,
                                   using the Chief Strategist's recommendation
                                   as context
   ↓
multi-agent-report_<portfolio>.json + .html (consolidated findings)
```

A sixth **Research Analyst** (WebSearch/WebFetch, dated news for the top
risk contributors) is designed but **not yet implemented**.
`supervisor.py:run_agent()` already has the branch for a web-enabled agent
(`allowed_tools`, `max_turns=30`); no such agent is defined in
`agent_skills.md` yet.

### Key Design Decisions

1.  **Offline-first with optional web layer**
    - All 5 current agents work from a static snapshot + general knowledge; none has web access
    - A WebSearch-enabled Research Analyst is designed but not yet built
    - Price data freshness is flagged but never auto-refreshed by this project — that's owned by whatever upstream pipeline produces `closing_prices.csv`/`volatility.csv`; a scheduled deployment can sync fresh input before each run (see README's reference deployment), but that's fresh *input*, not an in-process price refresh
2.  **Lane discipline (non-redundancy)**
    - Each agent has exclusive OWNS and explicit DEFER boundaries
    - Shared givens (coverage, denominators, data artifacts) prevent re-derivation
    - Consolidation removes duplication by design
3.  **Data source priority**
    - Price/vol CSVs supplied externally (e.g. from an R pipeline): `closing_prices.csv` / `volatility.csv`
    - Stale flag if >30 days old; agents told to note it but analysis proceeds
    - No live market data; no fundamental database calls

------------------------------------------------------------------------

## File Manifest

### Core Python Modules (`app/`)

#### `supervisor.py`

**Orchestrator, portfolio-data prep, and agent factory**
- `prepare_portfolio(excel_path, portfolio_name)` — reads `portfolio.xlsx` directly with pandas and returns the structured snapshot dict in memory (no intermediate JSON file)
- `validate_and_normalize()` — ensure weights > 0, sum to ~100%
- `calculate_risk_metrics()` — HHI, top-5/top-10, num positions, avg volatility/beta
- `build_sector_breakdown()` — group by sector, sum weights
- `build_risk_contributors()` / `build_top_risk_contributors()` / `build_all_positions()` — ranked position lists
- **Input columns (portfolio.xlsx):** ticker, name, weight_pct, sector, volatility, risk_contribution_pct, market_beta (falls back to legacy beta_to_portfolio / relative_volatility via `_beta_column()`)
- `check_data_freshness()` — read local CSVs, compute age_days, flag if stale
- `format_portfolio_context()` — render portfolio snapshot as markdown for agents; also renders a drawdown table (max + current, with peak date and a 🟢 near-peak / 🟡 recovering / 🔴 deep-drawdown status per `_compute_drawdown_status()`) and an optional off-portfolio "Strategic Watch" section when a `PriceAnalyzer` is available
- `load_agent_skills()` / `get_agent_skills()` — parse `app/agent_skills.md` (Markdown, no YAML dep) into `{shared_givens, meta_prompt, agents}`, cached
- `build_agents()` — returns the `agents` dict from `agent_skills.md` (role, task, instructions, model, optional `allowed_tools`)
  - Portfolio Analyst — haiku-4.5 (OWNS concentration, structure, gaps)
  - Market & Fundamental Analyst — haiku-4.5 (OWNS thesis, quality, valuation, secular risk)
  - Risk Analyst — haiku-4.5 (OWNS vol, beta, correlation, stress, data quality)
  - Chief Strategist / meta-analyst — sonnet-5 (synthesises the three, `meta_prompt`)
  - Rebalancing Analyst — haiku-4.5 (full-book trim/add/hold calls, sees the Chief Strategist's output)
- `run_agent(agent_name, agent_spec, portfolio_context)` — async, uses ClaudeAgentOptions
  - Offline agents (no `allowed_tools`): max_turns=6
  - Web-enabled agent path (max_turns=30, `allowed_tools=["WebSearch", "WebFetch"]`) exists in code but no agent currently uses it — see Research Analyst above
- `run_supervisor(excel_path, portfolio_name)` — read `portfolio.xlsx` via `prepare_portfolio()`, check freshness, load `PriceAnalyzer`, format context, run the 3 core agents in parallel via `asyncio.gather()`, run the meta-analyst (max_turns=5) sequentially on their combined output, then run the Rebalancing Analyst sequentially with the meta-analyst's recommendation added to context, consolidate, write JSON + HTML
- `consolidate_report()` — merge agent outputs into a single JSON report (adds `rebalancing_recommendation` whenever `agent_skills.md` defines a `rebalancing-analyst` block)
- `generate_html_report()` — self-contained tabbed HTML viewer (light/dark aware) written alongside the JSON report, including a "Rebalancing" tab
- **Dependencies:** anthropic, claude-agent-sdk, python-dotenv, pandas, openpyxl

#### `price_analyzer.py`

**Local price CSV reader and analytics**
- `PriceAnalyzer(prices_csv, volatility_csv)` — load closing_prices.csv + optional volatility.csv
- Groups by ticker, computes per-ticker analyses
- `get_data_freshness()` — read latest date from CSV, compare to now, flag if >30 days (pure CSV check; no API calls)
- `calculate_volatility(ticker, days=252)` — prefer pre-calculated, fallback to computed from prices; true log returns `ln(P_t/P_{t-1})`, annualized (× √252), in %
- `calculate_drawdown(ticker)` — max all-time drawdown: max(prices) to min, as %
- `calculate_current_drawdown(ticker)` — drawdown from the most recent peak (not all-time), returns peak_date too; feeds `supervisor.py`'s 🟢/🟡/🔴 status classifier
- `get_current_price(ticker)` — latest Adjusted_close + date
- `get_momentum(ticker, periods=20)` — % change over last N days
- `analyze_position(ticker)` / `analyze_multiple(tickers)` — comprehensive dict / batch wrapper
- **Dependencies:** pandas

#### `eodhd_client.py`

**EODHD REST API wrapper (prepared, not yet integrated)**
- `EODHDClient(api_key)` — init with EODHD_API_KEY from env
- `_throttle()` — enforce rate limit (0.6s min interval, ~1 call/sec)
- `_request()` — make rate-limited GET, handle HTML errors (subscription limits)
- `get_eod_prices(ticker, from_date, to_date)` — fetch EOD historical (free plan: max 365d)
- `get_current_price(ticker)` — fetch most recent EOD close
- Standalone `calculate_volatility(prices)` / `calculate_drawdown(prices)` — from a list of price dicts
- **Dependencies:** httpx, python-dotenv

#### `agent_skills.md`

**Agent roles + prompts, editable without touching Python**
- Parsed by `supervisor.py:load_agent_skills()` (line-based Markdown parser, no YAML dependency)
- Blocks: `## shared_givens`, `## meta_prompt` (with `$portfolio_structure`, `$market_and_fundamental`, `$risk_assessment` placeholders), and one `## agent:<key>` per agent (`model`/`role`/`allowed_tools` scalars + `### task` / `### instructions` fenced blocks) — currently `portfolio-analyst`, `market-fundamental-analyst`, `risk-analyst`, `rebalancing-analyst`, `meta-analyst`
- Path override: `AGENT_SKILLS_FILE` env var
- Shipped into the image via `COPY app/*.md /app/`

### Configuration (`config/`)

#### `env.example`

```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_WORKSPACE_ID=
EODHD_API_KEY=
PORTFOLIO_DATA=/data
PORTFOLIO_REPORTS=/reports
```

#### `.env` (created locally, never committed)

- `cp config/env.example config/.env`, fill in keys
- Read by `supervisor.py` and `eodhd_client.py` via `load_dotenv()`

### Docker (`Dockerfile`, `docker-compose.yml`)

- Base: `python:3.11-slim`
- Non-root user: `agent`
- Entry: `python /app/supervisor.py /data/portfolio.xlsx`
- Compose:
  - Mounts: `./data:/data:ro` (input), `./reports:/reports` (output)
  - Network/memory settings tuned for a low-resource reference deployment — adjust for your own host

### Data (`data/`)

- **Input:** `portfolio.xlsx` (Excel aggregated risk view) — read directly by `supervisor.py`, no intermediate file
- **Output:** `multi-agent-report_<portfolio>.json` — consolidation of all 5 agents' analyses
- **Supporting:** `closing_prices.csv`, `volatility.csv` (from an external pipeline, optional for the stale-check; `*.example.csv` files show the expected schema)

------------------------------------------------------------------------

## Key Technical Details

### Claude Agent SDK Integration

- **Models:** `claude-haiku-4-5-20251001` for 4 agents (Portfolio, Market & Fundamental, Risk, Rebalancing); `claude-sonnet-5` for the meta-analyst (Chief Strategist) synthesis step.
- **Execution order:** the 3 core agents run in parallel via `asyncio.gather()`; the meta-analyst runs sequentially on their combined output; the Rebalancing Analyst runs sequentially after that, with the meta-analyst's recommendation added to its context.
- **Options:**
  - `max_turns`: 6 (core offline agents), 5 (meta-analyst), 30 (web-tool code path — exists in `run_agent()` but unused, no agent currently sets `allowed_tools`)
  - `permission_mode: "bypassPermissions"` (sandbox trusted; no approval prompts)
  - `allowed_tools`: empty list for every agent currently defined in `agent_skills.md`

### Data Flow & Assumptions

- Coverage and concentration figures are computed from whatever `portfolio.xlsx` contains — if it's a partial book (e.g. an aggregated risk view rather than every account), every downstream metric is on that partial base, and agents are told so via `shared_givens`.
- `agent_skills.md`'s `shared_givens` block is the mechanism for telling every offline agent a fact once (so it isn't re-derived or contradicted five times) — e.g. a data-quality call the Risk Analyst made that other agents should treat as settled rather than re-litigate. Edit that block to match your own portfolio's real coverage and any data-quality findings.

### Shared Givens (prevent agent re-derivation)

Sourced from the `## shared_givens` block in `app/agent_skills.md` and injected into every offline agent's prompt. This is where you encode facts specific to *your* portfolio (coverage %, denominators, any data-quality findings already investigated) so agents cite them instead of re-deriving or second-guessing them each run. The shipped example ties this to a specific sample portfolio's coverage and volatility findings — replace it with your own before relying on the output.

------------------------------------------------------------------------

## Dependencies & Requirements

### Python Packages (`requirements.txt`)

```
anthropic
claude-agent-sdk
python-dotenv
pandas
openpyxl
httpx
```

### System Requirements

- Python 3.11+
- Docker + Docker Compose (for containerized/scheduled deployment)
- `ANTHROPIC_API_KEY` (mandatory)
- `EODHD_API_KEY` (optional, unused until Phase 3 below)
- Internet access (only needed once a web-enabled agent is added — see Roadmap)

------------------------------------------------------------------------

## Roadmap

**Done:**
- `supervisor.py` reads `portfolio.xlsx` directly (no intermediate JSON)
- `price_analyzer.py` reads local CSVs, flags staleness
- 5-agent pipeline (3 parallel specialists → Chief Strategist → Rebalancing Analyst), consolidated JSON + HTML report
- Docker image builds and runs; a reference weekly-scheduled NAS deployment is documented in the README

**Not yet done:**
- **EODHD integration:** wire `eodhd_client.py` into `price_analyzer.py`'s fallback path so stale prices (>30 days) can be refreshed on demand, with local caching and the existing rate limit (0.6s min interval)
- **Research Analyst:** a WebSearch/WebFetch-enabled 6th agent for dated news on top risk contributors — `run_agent()` already has the code path (`allowed_tools`, `max_turns=30`), just no agent block defined yet
- **Multi-portfolio orchestration:** running several `<name>.xlsx` files in one invocation rather than one process per portfolio
- **Report versioning:** reports are overwritten in place each run; no built-in history
- No automated test suite yet

------------------------------------------------------------------------

## Debugging & Monitoring

### Common Issues

**Missing ANTHROPIC_API_KEY**

```bash
# Error: ANTHROPIC_API_KEY not set
export ANTHROPIC_API_KEY=sk-ant-...
# Or add to config/.env
```

**Price CSV not found** — `closing_prices.csv` should be in `./data/` or `/data` (Docker mount). If missing, the freshness check returns `None` and agents proceed without a staleness warning; there's no automatic EODHD refresh yet, so a stale flag is a manual signal to re-run whatever upstream pipeline produces these CSVs.

**Docker networking:** some NAS/DSM6 setups require `--network=host` for containers to reach the internet; test whether your host needs it before assuming so.

**Memory-constrained hosts:** if you're running on a low-RAM NAS or VM, reduce `max_turns`, simplify the portfolio, or raise `memswap_limit` in `docker-compose.yml` rather than the hard memory limit.

### Logs & Output

- stderr: progress messages (✓ loaded, ✗ errors)
- stdout: JSON reports (stdout + file write)

------------------------------------------------------------------------

## Architecture Rationale

**Why offline-first with optional web?**
- Reduces API calls (cheaper, faster, deterministic)
- Agents working from one static snapshot = reproducible output
- A future research layer (web) would be explicitly bounded (top risk contributors only, a narrow date window)
- Freshness check is transparent; stale data is flagged but analysis proceeds rather than blocking

**Why lane discipline?**
- Prevents convergence: N agents on the same data without lanes = N repeats of the same analysis
- Shared givens let downstream agents cite without re-deriving
- Consolidation removes duplication by design, not post-hoc editing

**Why async agents?**
- Runs in parallel; faster wall-clock time
- Claude Agent SDK `asyncio.gather()` orchestrates cleanly
- Scales to more agents without a proportional slowdown, as long as sequencing (meta-analyst needs the three specialists; Rebalancing Analyst needs the meta-analyst) is respected

------------------------------------------------------------------------

## Future Extensibility

- **Multi-portfolio:** Run `supervisor.py` with different `<name>.xlsx` files; parallelize via shell or scheduler
- **Scheduled runs:** cron or your platform's task scheduler, with stale-check + EODHD refresh once Phase 3 lands
- **Custom agents:** add lane-specific agents (e.g., ESG Analyst, Volatility Forecaster) by extending `agent_skills.md` and `build_agents()`
- **Report versioning:** store `multi-agent-report_*.json` with a timestamp suffix for trend analysis
- **Alert system:** parse report findings, trigger alerts if concentration HHI exceeds a threshold or volatility exceeds N standard deviations

------------------------------------------------------------------------

## References

- **Claude Agent SDK:** async query loop, model selection, tool access control
- **EODHD:** historical EOD prices, rate limits, free-plan constraints
- **Portfolio data source:** bring your own — this project expects an Excel aggregated risk view, but any pipeline producing the documented columns works
