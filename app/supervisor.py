"""
Portfolio Multi-Agent Supervisor.

Orchestrates five Claude agents (roles/prompts defined in
app/agent_skills.md):
  1. Portfolio Analyst              - structure, concentration, diversification
  2. Market & Fundamental Analyst   - regimes, exposures, valuations, margins
  3. Risk Analyst                   - volatility, correlations, drawdowns
  4. Chief Strategist (meta-analyst) - synthesizes the three into a
                                        recommendation (runs sequentially,
                                        after the first three run in parallel)
  5. Rebalancing Analyst            - full-book trim/add/hold recommendation;
                                        runs last, every run, unconditionally
                                        (no env flag, no trigger)

All five work offline, from the prepared portfolio snapshot plus general
knowledge - none has web access. A web-search-enabled Research Analyst
(top-5 risk contributors) is designed but not yet implemented.

Price/volatility inputs are read from local CSVs first (closing_prices.csv,
volatility.csv from the R pipeline). If those prices are older than 30 days
the supervisor tells every agent to flag it in the report — it never triggers
an automatic EODHD refresh.

Each agent receives prepared portfolio data (JSON) and produces analysis.
Supervisor consolidates findings into a unified report.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from string import Template
from typing import Any

import markdown as markdown_lib
import pandas as pd
from dotenv import load_dotenv
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

try:
    from price_analyzer import PriceAnalyzer
except ImportError:  # running as package
    from app.price_analyzer import PriceAnalyzer


# --- Portfolio data prep (Excel -> in-memory snapshot dict) ---------------
# No external API calls; all data comes from the input file. Read straight
# from portfolio.xlsx on every run — no intermediate analysis_input.json.

# Beta column emitted by the R pipeline. Current reports write `market_beta`
# (a genuine CAPM beta vs. MSCI World / URTH). Older reports wrote
# `beta_to_portfolio` (actually a standalone-vol ratio, since renamed to
# `relative_volatility`); keep it as a fallback so stale files still parse.
BETA_COLUMNS = ("market_beta", "beta_to_portfolio", "relative_volatility")


def _beta_column(df: pd.DataFrame) -> str | None:
    """First recognised beta column present in the frame, else None."""
    return next((c for c in BETA_COLUMNS if c in df.columns), None)


def validate_and_normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure weights sum to ~100%, clean data."""
    df = df.copy()
    df["weight_pct"] = pd.to_numeric(df["weight_pct"], errors="coerce").fillna(0)

    total = df["weight_pct"].sum()
    if total <= 0:
        raise ValueError(f"Total weight is {total}, expected > 0")

    if abs(total - 100.0) > 1.0:
        print(f"⚠ Warning: weights sum to {total:.2f}% (expected ~100%)", file=sys.stderr)

    return df


def calculate_risk_metrics(df: pd.DataFrame) -> dict[str, Any]:
    """Calculate concentration and risk metrics from aggregated portfolio."""
    weights = df["weight_pct"].values / 100  # [0,1] for HHI
    hhi = (weights ** 2).sum() * 10000

    top_5 = df.nlargest(5, "weight_pct")["weight_pct"].sum()
    top_10 = df.nlargest(10, "weight_pct")["weight_pct"].sum()

    # Portfolio-level risk metrics
    total_risk = df["risk_contribution_pct"].sum() if "risk_contribution_pct" in df.columns else 0
    avg_volatility = df["volatility"].mean() if "volatility" in df.columns else 0
    beta_col = _beta_column(df)
    avg_beta = df[beta_col].mean() if beta_col else 0

    return {
        "hhi": round(hhi, 1),
        "top_5_pct": round(top_5, 2),
        "top_10_pct": round(top_10, 2),
        "num_positions": len(df),
        "total_portfolio_risk_pct": round(total_risk, 2),
        "avg_volatility": round(avg_volatility, 2),
        "avg_beta": round(avg_beta, 2),
    }


def build_sector_breakdown(df: pd.DataFrame) -> dict[str, float]:
    """Sector-wise weights; group unclassified."""
    if "sector" not in df.columns:
        return {"All": 100.0}

    sector_weights = df.groupby("sector", dropna=False)["weight_pct"].sum()

    result = {}
    for sector, weight in sector_weights.items():
        key = sector if pd.notna(sector) else "Unclassified"
        result[key] = round(weight, 2)

    return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))


def _rank_positions(df: pd.DataFrame, rank_col: str, top_n: int | None = None) -> list[dict[str, Any]]:
    """Rank positions by `rank_col`, returning the shared per-ticker dict shape.

    `top_n=None` returns every position (the full book); an int caps it.
    """
    beta_col = _beta_column(df)
    has_index = "index" in df.columns
    cols = ["ticker", "name", "weight_pct", "sector", "index", "volatility", "risk_contribution_pct"]
    if beta_col:
        cols.append(beta_col)
    available_cols = [c for c in cols if c in df.columns]
    n = top_n if top_n is not None else len(df)
    top = df.nlargest(n, rank_col)[available_cols].copy()

    return [
        {
            "ticker": row.get("ticker", ""),
            "name": row.get("name", ""),
            "weight_pct": row.get("weight_pct", 0),
            "sector": row.get("sector", ""),
            # Source index/fund a position is sleeved through (e.g. "AmundiCoreWorld",
            # "iSharesEMIMI") — tells the Rebalancing Analyst which vehicle a trim/add
            # would actually go through. Optional column; None where absent (e.g. a
            # second portfolio doesn't have it yet).
            "index": row.get("index", None) if has_index else None,
            "volatility": row.get("volatility", None),
            "risk_contribution_pct": row.get("risk_contribution_pct", None),
            "beta": row.get(beta_col, None) if beta_col else None,
        }
        for _, row in top.iterrows()
    ]


def _rank_by_risk(df: pd.DataFrame, top_n: int | None = None) -> list[dict[str, Any]]:
    """Rank all positions by risk_contribution_pct (fallback: weight_pct).

    `top_n=None` returns every position (used to search past a fixed cutoff
    when covering a target share of total risk); an int caps it like before.
    """
    rank_col = "risk_contribution_pct" if "risk_contribution_pct" in df.columns else "weight_pct"
    return _rank_positions(df, rank_col, top_n=top_n)


def build_risk_contributors(df: pd.DataFrame, top_n: int = 15) -> list[dict[str, Any]]:
    """Top risk contributors, ranked by risk_contribution_pct (fallback: weight_pct)."""
    return _rank_by_risk(df, top_n=top_n)


def build_all_positions(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Every position in the book, ranked by weight_pct — the full-portfolio
    view for the Rebalancing Analyst (which must see every ticker, not just
    the top risk contributors that make up ~50% of total risk)."""
    return _rank_positions(df, "weight_pct", top_n=None)


def build_top_risk_contributors(
    df: pd.DataFrame,
    total_portfolio_risk_pct: float,
    min_count: int = 5,
    min_coverage_pct: float = 50.0,
) -> list[dict[str, Any]]:
    """The names driving portfolio risk: at least `min_count`, extended further
    if needed until they jointly cover `min_coverage_pct` of the portfolio's
    total risk contribution (not a fixed top-5 — a flatter risk distribution
    needs more names to reach the same coverage).

    Coverage is measured against `total_portfolio_risk_pct` (the sum of
    risk_contribution_pct across the whole portfolio, from
    calculate_risk_metrics()), not an absolute percentage-point cutoff — e.g.
    "covers 50%" means 50% of the portfolio's own total risk, whatever that
    total is.
    """
    ranked = _rank_by_risk(df, top_n=None)
    if not ranked:
        return []

    target = (min_coverage_pct / 100.0) * total_portfolio_risk_pct if total_portfolio_risk_pct else 0.0
    cumulative = 0.0
    selected: list[dict[str, Any]] = []
    for holding in ranked:
        selected.append(holding)
        cumulative += holding.get("risk_contribution_pct") or 0
        if len(selected) >= min_count and cumulative >= target:
            break
    return selected


def prepare_portfolio(excel_path: Path, portfolio_name: str = "portfolio") -> dict[str, Any]:
    """Read aggregated portfolio.xlsx into a structured snapshot dict, ready
    for multi-agent analysis. No external calls; pure data wrangling."""
    if not excel_path.exists():
        raise FileNotFoundError(f"{excel_path} not found")

    df = pd.read_excel(excel_path)
    print(f"✓ Loaded {len(df)} positions from {excel_path.name}", file=sys.stderr)

    # Filter: drop zero-weight positions
    df = df[(df["weight_pct"] > 0)].copy()
    print(f"✓ After filtering: {len(df)} positions", file=sys.stderr)

    df = validate_and_normalize(df)

    risk_metrics = calculate_risk_metrics(df)
    sector_breakdown = build_sector_breakdown(df)
    risk_contributors = build_risk_contributors(df, top_n=15)
    # Not a fixed top-5: at least 5 names, extended further if needed to cover
    # >=50% of total portfolio risk (see build_top_risk_contributors()). Field
    # name kept as `top_5_by_risk` for backward compatibility with existing
    # reports/consumers even though it may now hold more than 5 entries.
    top_5_by_risk = build_top_risk_contributors(df, risk_metrics["total_portfolio_risk_pct"])
    all_positions = build_all_positions(df)

    return {
        "portfolio_name": portfolio_name,
        "data_type": "aggregated_risk_view",
        "metadata": {
            "num_positions": risk_metrics["num_positions"],
            "total_weight_pct": round(df["weight_pct"].sum(), 2),
            "data_source": "Aggregated portfolio.xlsx (risk key drivers)",
        },
        "concentration": {
            "hhi": risk_metrics["hhi"],
            "top_5_pct": risk_metrics["top_5_pct"],
            "top_10_pct": risk_metrics["top_10_pct"],
        },
        "risk_profile": {
            "portfolio_risk_pct": risk_metrics["total_portfolio_risk_pct"],
            "avg_volatility": risk_metrics["avg_volatility"],
            "avg_beta": risk_metrics["avg_beta"],
        },
        "sector_breakdown": sector_breakdown,
        "risk_contributors": risk_contributors,
        "top_5_by_risk": top_5_by_risk,
        "all_positions": all_positions,
    }


def _find_price_csvs() -> tuple[Path | None, Path | None]:
    """Locate closing_prices.csv / volatility.csv from env or known layouts."""
    candidates = []
    if os.environ.get("PRICE_DATA"):
        candidates.append(Path(os.environ["PRICE_DATA"]))
    here = Path(__file__).resolve().parent
    candidates += [
        Path("/data"),                                 # docker mount
        here.parent / "data",                           # multi-agent/data
        here.parent.parent / "portfolio" / "meta",      # R repo meta/ (local mac)
    ]
    for base in candidates:
        prices = base / "closing_prices.csv"
        if prices.exists():
            vol = base / "volatility.csv"
            return prices, (vol if vol.exists() else None)
    return None, None


# R's portfolio_report.Rmd (scripts/correlation_analysis.R was deleted as
# orphaned — the report now exports the correlation CSV directly) uses
# "user1"/"user2" as portfolio ids, per portfolio/config/portfolio_configs.csv.
# This project's scheduled run calls the same underlying holdings "portfolio"
# (see pull-to-nas.sh / run_weekly_analysis.sh); config confirms user1 ->
# output_file portfolio.xlsx, i.e. the exact file this project reads. "user2"
# happens to match on both sides already (example naming — substitute your own
# portfolio ids).
_R_PORTFOLIO_ID = {"portfolio": "user1"}


def _find_correlation_csv(portfolio_name: str) -> Path | None:
    """Locate correlation_matrix_<r_id>.csv from env or known layouts (same
    search order as _find_price_csvs()). Produced by portfolio_report.Rmd's
    correlation-heatmap chunk (long format: ticker1, ticker2, correlation),
    one file per portfolio id so multiple portfolios don't clobber each other —
    see pull-to-nas.sh's CORRELATION_SRC. Optional; the pipeline runs fine
    without it."""
    filename = f"correlation_matrix_{_R_PORTFOLIO_ID.get(portfolio_name, portfolio_name)}.csv"
    candidates = []
    if os.environ.get("CORRELATION_DATA"):
        candidates.append(Path(os.environ["CORRELATION_DATA"]))
    here = Path(__file__).resolve().parent
    candidates += [
        Path("/data"),                                       # docker mount
        here.parent / "data",                                 # multi-agent/data
        here.parent.parent / "portfolio" / "data" / "clean",  # R repo (local mac)
    ]
    for base in candidates:
        f = base / filename
        if f.exists():
            return f
    return None


def load_top_correlations(csv_path: Path, tickers: list[str]) -> list[dict[str, Any]]:
    """Pairwise correlations among `tickers`, from a long-format
    ticker1/ticker2/correlation CSV (as written by correlation_analysis.R).

    Returns one row per unordered pair (self-pairs and the mirrored
    duplicate that a symmetric matrix produces are dropped), sorted by
    |correlation| descending — the highest-magnitude pairs (positive or
    negative) are what a risk read cares about first.
    """
    df = pd.read_csv(csv_path)
    wanted = set(tickers)
    df = df[df["ticker1"].isin(wanted) & df["ticker2"].isin(wanted) & (df["ticker1"] != df["ticker2"])]

    seen: set[tuple[str, str]] = set()
    pairs: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        pair = tuple(sorted((str(row["ticker1"]), str(row["ticker2"]))))
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append({"ticker1": pair[0], "ticker2": pair[1], "correlation": float(row["correlation"])})

    pairs.sort(key=lambda p: abs(p["correlation"]), reverse=True)
    return pairs


def check_data_freshness() -> dict[str, Any] | None:
    """Read local price CSVs and report how stale they are. No API calls."""
    prices_csv, vol_csv = _find_price_csvs()
    if prices_csv is None:
        print("⚠ No closing_prices.csv found — skipping freshness check", file=sys.stderr)
        return None
    try:
        analyzer = PriceAnalyzer(prices_csv, vol_csv)
        fresh = analyzer.get_data_freshness()
        msg = f"Price data: latest {fresh['latest_date']}, {fresh['age_days']}d old"
        print(f"✓ {msg} (stale={fresh['is_stale']})", file=sys.stderr)
        return fresh
    except Exception as e:  # noqa: BLE001 - freshness must never break the run
        print(f"⚠ Freshness check failed: {e}", file=sys.stderr)
        return None


def _safe_check_optional(obj, *field_names) -> bool:
    """
    Check if object and optional fields exist.

    Usage:
      _safe_check_optional(analyzer)  # True if not None
      _safe_check_optional(data, "risk_profile")  # True if dict has key
    """
    if obj is None:
        return False
    for field in field_names:
        if isinstance(obj, dict) and field not in obj:
            return False
        if not isinstance(obj, dict) and not hasattr(obj, field):
            return False
    return True


def _compute_drawdown_status(current_pct: float, max_pct: float | None = None) -> str:
    """
    Classify drawdown severity into status emoji + label.

    Thresholds:
    - current_pct < 5%: "🟢 Near peak"
    - current_pct > max_pct * 0.8: "🔴 Deep drawdown"
    - else: "🟡 Recovering"
    """
    if max_pct is None:
        max_pct = 100

    if current_pct < 5:
        return "🟢 Near peak"
    elif current_pct > max_pct * 0.8:
        return "🔴 Deep drawdown"
    else:
        return "🟡 Recovering"


def format_portfolio_context(
    data: dict[str, Any],
    freshness: dict[str, Any] | None = None,
    analyzer: PriceAnalyzer | None = None,
    include_all_positions: bool = False,
    correlations: list[dict[str, Any]] | None = None,
) -> str:
    """Format portfolio data as readable context for agents.

    Args:
        data: Prepared portfolio JSON
        freshness: Data freshness dict from price_analyzer
        analyzer: PriceAnalyzer instance (optional, for drawdown analysis)
        include_all_positions: append a full-book table (every ticker in the
            .xlsx, not just the top risk contributors). Off by default — the
            3 core analysts only need the risk-weighted subset; the
            Rebalancing Analyst is the one consumer that must see every
            position, since a rebalance call can't ignore small positions.
        correlations: pairwise correlations among the top risk contributors,
            from load_top_correlations() (optional — R's correlation
            matrix isn't always available/synced).
    """
    md = f"""# Portfolio: {data['portfolio_name']}

## Overview
- Data Type: {data.get('data_type', 'unknown')}
- Positions: {data['metadata']['num_positions']}
- Total weight: {data['metadata']['total_weight_pct']}%

## Concentration Risk
*HHI and Top-N below are computed on this analyzed subset only —
{data['metadata']['num_positions']} positions, {data['metadata']['total_weight_pct']}% of
the full book — not comparable to a full-portfolio HHI computed elsewhere (e.g. the R
pipeline's own report), which covers every holding and uses a 0–1 scale rather than
0–10000.*
- HHI: {data['concentration']['hhi']:.1f} (0=perfect diversification, 10000=one position)
- Top 5: {data['concentration']['top_5_pct']}%
- Top 10: {data['concentration']['top_10_pct']}%
"""

    # Data freshness — prices/volatilities come from local CSVs (no live API)
    if _safe_check_optional(freshness):
        md += "\n## Data Freshness\n"
        md += "- Source: local CSV (closing_prices.csv / volatility.csv), no live API\n"
        md += f"- Latest price date: {freshness['latest_date']} ({freshness['age_days']} days old)\n"
        if freshness["is_stale"]:
            md += (
                f"- STALE: exceeds the {freshness['threshold_days']}-day threshold. "
                f"State clearly in your analysis that the price/volatility inputs are "
                f"{freshness['age_days']} days old and may not reflect current market "
                f"conditions. No automatic data refresh is performed.\n"
            )
        else:
            md += f"- Within the {freshness['threshold_days']}-day freshness threshold.\n"

    # Risk Profile (if available)
    if _safe_check_optional(data, "risk_profile"):
        md += f"""
## Portfolio Risk Profile
- Portfolio Risk: {data['risk_profile'].get('portfolio_risk_pct', 0)}%
- Average Volatility: {data['risk_profile'].get('avg_volatility', 0):.2f}%
- Average Beta: {data['risk_profile'].get('avg_beta', 0):.2f}
"""

    # Sector Breakdown
    if _safe_check_optional(data, "sector_breakdown"):
        md += "\n## Sector Breakdown\n"
        for sector, weight in list(data["sector_breakdown"].items())[:10]:
            md += f"- {sector}: {weight}%\n"

    # Risk Contributors (aggregated view)
    if _safe_check_optional(data, "risk_contributors"):
        md += "\n## Top Risk Contributors\n"
        md += "| Ticker | Name | Weight | Sector | Vol | Risk Contrib | Beta |\n"
        md += "|--------|------|--------|--------|-----|--------------|------|\n"
        for h in data["risk_contributors"][:15]:
            ticker = h.get("ticker", "—")
            name = (h.get("name", "")[:20]) or "—"
            weight = f"{h.get('weight_pct', 0):.1f}" if h.get('weight_pct') else "—"
            sector = h.get("sector", "—")
            vol = f"{h.get('volatility', 0):.1f}" if h.get('volatility') is not None else "—"
            risk = f"{h.get('risk_contribution_pct', 0):.2f}" if h.get('risk_contribution_pct') is not None else "—"
            beta = f"{h.get('beta', 0):.2f}" if h.get('beta') is not None else "—"
            md += f"| {ticker} | {name} | {weight}% | {sector} | {vol}% | {risk}% | {beta} |\n"

    # Top risk contributors (>=5 names, extended further if needed to cover
    # >=50% of total portfolio risk — see build_top_risk_contributors() above)
    top5 = data.get("top_5_by_risk") or data.get("risk_contributors", [])[:5]
    if top5:
        md += f"\n## Top {len(top5)} by Weighted Risk Contribution\n"
        for i, h in enumerate(top5, 1):
            rc = h.get("risk_contribution_pct")
            rc_s = f"{rc:.2f}%" if rc is not None else "—"
            md += (
                f"{i}. {h.get('ticker', '—')} — {h.get('name', '')} "
                f"(risk contribution {rc_s}, weight {h.get('weight_pct', 0):.2f}%, "
                f"sector {h.get('sector', '—')})\n"
            )

    # Pairwise correlations among the top risk contributors (if R's
    # correlation matrix is available — see load_top_correlations()). This is
    # real, computed data, not a qualitative guess: use it instead of
    # reasoning about co-movement from general knowledge.
    if correlations:
        md += f"\n## Pairwise Correlations — Top {len(top5)} Risk Contributors\n"
        md += "| Ticker 1 | Ticker 2 | Correlation |\n"
        md += "|----------|----------|-------------|\n"
        for p in correlations:
            md += f"| {p['ticker1']} | {p['ticker2']} | {p['correlation']:.2f} |\n"

    # Drawdown analysis for top risk contributors (if price data available)
    if _safe_check_optional(analyzer):
        md += f"\n## Drawdown Analysis — Top {len(top5)} Risk Contributors\n"
        md += "| Ticker | Max DD (all-time) | Current DD | Peak Date | Status |\n"
        md += "|--------|-------------------|-----------|-----------|--------|\n"
        try:
            for h in top5:
                ticker = h.get("ticker")
                if not ticker:
                    continue
                try:
                    max_dd = analyzer.calculate_drawdown(ticker)
                    curr_dd = analyzer.calculate_current_drawdown(ticker)

                    max_dd_val = f"{max_dd.get('max_drawdown_pct', '—')}%" if max_dd.get('error') is None else "—"
                    curr_dd_val = f"{curr_dd.get('current_drawdown_pct', '—')}%" if curr_dd.get('error') is None else "—"
                    peak_date = curr_dd.get('peak_date', '—') if curr_dd.get('error') is None else "—"

                    # Status indicator: is it near all-time low or recovering?
                    if curr_dd.get('error') is None and max_dd.get('error') is None:
                        curr_val = curr_dd.get('current_drawdown_pct', 0)
                        max_val = max_dd.get('max_drawdown_pct', 0)
                        status = _compute_drawdown_status(curr_val, max_val)
                    else:
                        status = "—"

                    md += f"| {ticker} | {max_dd_val} | {curr_dd_val} | {peak_date} | {status} |\n"
                except Exception as e:
                    # Graceful fallback if ticker analysis fails
                    md += f"| {ticker} | — | — | — | Error: {str(e)[:20]} |\n"
        except Exception as e:
            # Graceful fallback if drawdown analysis fails entirely
            md += f"\n*Note: Drawdown analysis unavailable ({str(e)[:40]})*\n"

    # Strategic watch: Bitcoin (independent of portfolio)
    if _safe_check_optional(analyzer):
        md += "\n## Strategic Watch: Bitcoin (Off-Portfolio)\n"
        md += "*Bitcoin tracked independently; not included in portfolio weights or concentration metrics.*\n"
        try:
            btc_ticker = "BTC"
            if btc_ticker in analyzer.by_ticker:
                btc_current = analyzer.get_current_price(btc_ticker)
                btc_momentum = analyzer.get_momentum(btc_ticker, periods=20)
                btc_max_dd = analyzer.calculate_drawdown(btc_ticker)
                btc_curr_dd = analyzer.calculate_current_drawdown(btc_ticker)
                btc_vol = analyzer.calculate_volatility(btc_ticker)

                if btc_current.get('error') is None:
                    price = f"${btc_current.get('price', '—'):,.0f}" if btc_current.get('price') else "—"
                    price_date = btc_current.get('date', '—')
                else:
                    price = "—"
                    price_date = "—"

                if btc_momentum.get('error') is None:
                    momentum = f"{btc_momentum.get('momentum_pct', 0):+.1f}%" if btc_momentum.get('momentum_pct') is not None else "—"
                else:
                    momentum = "—"

                if btc_vol.get('error') is None:
                    volatility = f"{btc_vol.get('volatility_pct', 0):.1f}%" if btc_vol.get('volatility_pct') is not None else "—"
                else:
                    volatility = "—"

                if btc_max_dd.get('error') is None and btc_curr_dd.get('error') is None:
                    max_dd_btc = f"{btc_max_dd.get('max_drawdown_pct', 0):.1f}%"
                    curr_dd_btc = f"{btc_curr_dd.get('current_drawdown_pct', 0):.1f}%"
                    peak_date_btc = btc_curr_dd.get('peak_date', '—')

                    curr_val = btc_curr_dd.get('current_drawdown_pct', 0)
                    max_val = btc_max_dd.get('max_drawdown_pct', 0)
                    btc_status = _compute_drawdown_status(curr_val, max_val)
                else:
                    max_dd_btc = "—"
                    curr_dd_btc = "—"
                    peak_date_btc = "—"
                    btc_status = "—"

                md += f"""
- **Price:** {price} (as of {price_date})
- **20-day momentum:** {momentum}
- **Volatility (annualized):** {volatility}
- **Max drawdown (all-time):** {max_dd_btc}
- **Current drawdown (from peak):** {curr_dd_btc} (peak: {peak_date_btc}) {btc_status}

**Context:** Bitcoin serves as a non-correlated alternative asset (typically 0.0-0.3 correlation with equities).
Consider as a diversification hedge against concentrated equity/tech positioning.
"""
            else:
                md += "\n*Bitcoin (BTC) data not found in price history.*\n"
        except Exception as e:
            md += f"\n*Bitcoin analysis unavailable ({str(e)[:50]})*\n"

    # Full-book position table — only for consumers that must reason about
    # every ticker (e.g. the Rebalancing Analyst), not just the risk-weighted
    # subset the other agents see above.
    if include_all_positions and _safe_check_optional(data, "all_positions"):
        all_positions = data["all_positions"]
        md += f"\n## All Positions ({len(all_positions)} tickers, full book)\n"
        md += "| Ticker | Name | Weight | Sector | Index/Source | Vol | Risk Contrib | Beta |\n"
        md += "|--------|------|--------|--------|--------------|-----|--------------|------|\n"
        for h in all_positions:
            ticker = h.get("ticker", "—")
            name = (h.get("name", "")[:20]) or "—"
            weight = f"{h.get('weight_pct', 0):.2f}" if h.get('weight_pct') else "—"
            sector = h.get("sector", "—")
            index_source = h.get("index") or "—"
            vol = f"{h.get('volatility', 0):.1f}" if h.get('volatility') is not None else "—"
            risk = f"{h.get('risk_contribution_pct', 0):.2f}" if h.get('risk_contribution_pct') is not None else "—"
            beta = f"{h.get('beta', 0):.2f}" if h.get('beta') is not None else "—"
            md += f"| {ticker} | {name} | {weight}% | {sector} | {index_source} | {vol}% | {risk}% | {beta} |\n"

    return md


# Agent roles, prompts, the shared-givens block and the meta-analyst synthesis
# prompt all live in agent_skills.md (Markdown, next to this file) so they can be
# tuned without touching Python. Override the path with AGENT_SKILLS_FILE.
AGENT_SKILLS_FILE = Path(
    os.environ.get("AGENT_SKILLS_FILE", "")
    or Path(__file__).resolve().with_name("agent_skills.md")
)


def _first_fenced_block(body_lines: list[str]) -> str:
    """Return the text inside the first ``` fenced block of `body_lines`."""
    collected: list[str] = []
    inside = False
    for line in body_lines:
        if line.lstrip().startswith("```"):
            if inside:
                return "\n".join(collected)
            inside = True
            continue
        if inside:
            collected.append(line)
    raise ValueError("expected a fenced code block, found none")


def _parse_agent_block(body_lines: list[str]) -> dict[str, Any]:
    """Turn one `## agent:<key>` block into a spec dict.

    Scalar `field: value` lines before the first `### ` heading become string
    keys; each `### <name>` subsection takes its value from the fenced block
    that follows it. `allowed_tools` is split on commas into a list.
    """
    spec: dict[str, Any] = {}
    section: str | None = None
    section_lines: list[str] = []
    scalar_lines: list[str] = []
    for line in body_lines:
        if line.startswith("### "):
            if section is not None:
                spec[section] = _first_fenced_block(section_lines)
            section = line[4:].strip()
            section_lines = []
        elif section is None:
            scalar_lines.append(line)
        else:
            section_lines.append(line)
    if section is not None:
        spec[section] = _first_fenced_block(section_lines)

    for raw in scalar_lines:
        item = raw.strip()
        if not item or item.startswith(("<!--", "-", ">", "|")):
            continue
        if ":" in item:
            key, value = item.split(":", 1)
            spec[key.strip()] = value.strip()

    if isinstance(spec.get("allowed_tools"), str):
        spec["allowed_tools"] = [
            t.strip() for t in spec["allowed_tools"].split(",") if t.strip()
        ]
    return spec


def load_agent_skills(path: Path | None = None) -> dict[str, Any]:
    """Parse agent_skills.md into {shared_givens, meta_prompt, agents}.

    Dependency-free by design (no YAML): the file is Markdown so the prompts
    stay readable and diff cleanly. The parser only needs level-2 section
    headings (`## `) and fenced code blocks; a heading inside a fence is
    treated as literal text.
    """
    path = Path(path) if path is not None else AGENT_SKILLS_FILE
    if not path.exists():
        raise FileNotFoundError(f"agent skills file not found: {path}")

    blocks: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if current is not None:
                blocks[current].append(line)
            continue
        if not in_fence and line.startswith("## ") and not line.startswith("### "):
            current = line[3:].strip()
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].append(line)

    skills: dict[str, Any] = {"shared_givens": "", "meta_prompt": "", "agents": {}}
    for name, body in blocks.items():
        if name == "shared_givens":
            skills["shared_givens"] = _first_fenced_block(body)
        elif name == "meta_prompt":
            skills["meta_prompt"] = _first_fenced_block(body)
        elif name.startswith("agent:"):
            skills["agents"][name[len("agent:"):].strip()] = _parse_agent_block(body)

    if not skills["agents"]:
        raise ValueError(f"no agent blocks parsed from {path}")
    return skills


_SKILLS_CACHE: dict[str, Any] | None = None


def get_agent_skills() -> dict[str, Any]:
    """Load and cache the parsed agent_skills.md file."""
    global _SKILLS_CACHE
    if _SKILLS_CACHE is None:
        _SKILLS_CACHE = load_agent_skills()
    return _SKILLS_CACHE


def build_agents() -> dict[str, dict[str, Any]]:
    """Agent definitions (portfolio → market/fundamental → risk → meta).

    Loaded from agent_skills.md; see load_agent_skills().
    """
    return get_agent_skills()["agents"]


async def run_agent(
    agent_name: str,
    agent_spec: dict[str, Any],
    portfolio_context: str,
) -> str:
    """Run a single agent and return its response."""
    allowed_tools = agent_spec.get("allowed_tools", [])
    if allowed_tools:
        givens = ""
        closing = (
            "Use the available web tools to gather current information, then "
            "provide a dated, sourced summary within your lane. No advice."
        )
        max_turns = 30
    else:
        givens = f"\n{get_agent_skills()['shared_givens']}\n"
        closing = (
            "Answer in clear, simple sentences. 2–4 sentences total. "
            "Your response is merged with two other analysts; stay in your lane. "
            "No bullet points, no jargon. Be direct and concise. No investment advice."
        )
        max_turns = 6

    prompt = f"""You are a {agent_spec['role']}.

{agent_spec['instructions']}
{givens}
Portfolio data:
{portfolio_context}

---
{closing}
"""

    options = ClaudeAgentOptions(
        model=agent_spec["model"],
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        allowed_tools=allowed_tools,
        # Hermetic: don't let the agent pick up the operator's own
        # ~/.claude/CLAUDE.md or this repo's CLAUDE.md — it must be driven
        # only by the prompt built above (shared_givens + portfolio_context).
        setting_sources=[],
    )

    print(f"\n[{agent_name}] Running...", file=sys.stderr)

    final_result = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            final_result = message

    if final_result is None:
        raise RuntimeError(f"No result from {agent_name}")

    return final_result.result


async def run_supervisor(excel_path: Path, portfolio_name: str = "portfolio") -> None:
    """Run 3 agents, then a meta-analyst (Sonnet), then the Rebalancing Analyst."""
    # Read portfolio.xlsx directly — no intermediate JSON file.
    data = prepare_portfolio(excel_path, portfolio_name)

    # Resolve the reports directory once — used to write this run's report at
    # the end.
    reports_dir_env = os.environ.get("PORTFOLIO_REPORTS")
    reports_dir = Path(reports_dir_env) if reports_dir_env else excel_path.parent
    report_json_path = reports_dir / f"multi-agent-report_{data['portfolio_name']}.json"

    # Read local price CSVs first; flag staleness, never auto-refresh
    freshness = check_data_freshness()

    # Load price analyzer for drawdown analysis (optional)
    analyzer = None
    try:
        prices_csv, vol_csv = _find_price_csvs()
        if prices_csv is not None:
            analyzer = PriceAnalyzer(prices_csv, vol_csv)
            print(f"✓ Price analyzer loaded for drawdown analysis", file=sys.stderr)
    except Exception as e:
        print(f"⚠ Price analyzer initialization failed (continuing without): {e}", file=sys.stderr)

    # Load pairwise correlations among the top risk contributors, if R's
    # correlation matrix has been synced (see load_top_correlations() and
    # pull-to-nas.sh's CORRELATION_SRC). No date column in the CSV itself,
    # so staleness is judged by file mtime, same 30-day threshold as the
    # price data.
    correlations = None
    try:
        corr_csv = _find_correlation_csv(data["portfolio_name"])
        if corr_csv is not None:
            age_days = (time.time() - corr_csv.stat().st_mtime) / 86400
            if age_days > 30:
                print(f"⚠ {corr_csv.name} is {age_days:.0f}d old (>30d) — skipping, too stale to trust", file=sys.stderr)
            else:
                top_tickers = [h["ticker"] for h in data.get("top_5_by_risk", []) if h.get("ticker")]
                correlations = load_top_correlations(corr_csv, top_tickers)
                print(f"✓ Loaded {len(correlations)} pairwise correlations ({age_days:.0f}d old)", file=sys.stderr)
        else:
            print("⚠ No correlation_matrix_*.csv found — skipping (Risk Analyst reasons qualitatively instead)", file=sys.stderr)
    except Exception as e:
        print(f"⚠ Correlation data load failed (continuing without): {e}", file=sys.stderr)

    portfolio_context = format_portfolio_context(data, freshness, analyzer, correlations=correlations)
    agents = build_agents()

    print(f"Portfolio: {data['portfolio_name']}", file=sys.stderr)
    print(f"Positions: {data['metadata']['num_positions']}", file=sys.stderr)

    # Run 3 core agents in parallel (meta-analyst and rebalancing-analyst run
    # sequentially afterwards — rebalancing-analyst needs the meta-analyst's
    # synthesis as context, so it can't join the parallel batch).
    core_agents = {k: v for k, v in agents.items() if k not in ("meta-analyst", "rebalancing-analyst")}
    planned = len(core_agents) + ("meta-analyst" in agents) + ("rebalancing-analyst" in agents)
    print(f"Running {planned} agents...", file=sys.stderr)
    results = {}
    tasks = [
        run_agent(name, spec, portfolio_context)
        for name, spec in core_agents.items()
    ]

    try:
        responses = await asyncio.gather(*tasks)
        for (name, _), response in zip(core_agents.items(), responses):
            results[name] = response
    except Exception as e:
        print(f"Error running core agents: {e}", file=sys.stderr)
        raise

    # Now run meta-analyst with the results from the 3 core agents.
    # Prompt template lives in agent_skills.md (## meta_prompt); safe_substitute
    # leaves any stray $tokens untouched.
    if "meta-analyst" in agents:
        meta_prompt = Template(get_agent_skills()["meta_prompt"]).safe_substitute(
            portfolio_structure=results.get("portfolio-analyst", ""),
            market_and_fundamental=results.get("market-fundamental-analyst", ""),
            risk_assessment=results.get("risk-analyst", ""),
        )

        meta_spec = agents["meta-analyst"]
        options = ClaudeAgentOptions(
            model=meta_spec["model"],
            max_turns=5,
            permission_mode="bypassPermissions",
            allowed_tools=[],
            setting_sources=[],
        )

        print(f"\n[meta-analyst] Running...", file=sys.stderr)
        final_result = None
        async for message in query(prompt=meta_prompt, options=options):
            if isinstance(message, ResultMessage):
                final_result = message

        if final_result is None:
            results["meta-analyst"] = "No recommendation available."
        else:
            results["meta-analyst"] = final_result.result

    # Rebalancing Analyst — runs every time (mandatory, no env flag, no
    # trigger). Unlike the 3 core agents it must see the FULL position list
    # (every ticker in the .xlsx), not just the top risk contributors the
    # core agents work from. It also runs last so it can factor in the Chief
    # Strategist's synthesis.
    if "rebalancing-analyst" in agents:
        full_context = format_portfolio_context(
            data, freshness, analyzer, include_all_positions=True, correlations=correlations
        )
        full_context += f"\n## Chief Strategist Recommendation (context only)\n{results.get('meta-analyst', '')}\n"

        print(f"\n[rebalancing-analyst] Running...", file=sys.stderr)
        results["rebalancing-analyst"] = await run_agent(
            "rebalancing-analyst", agents["rebalancing-analyst"], full_context
        )

    # Consolidate into report
    report = consolidate_report(data, results, freshness)

    # Output JSON — reports_dir/report_json_path resolved at the top of this
    # function (PORTFOLIO_REPORTS if set, otherwise alongside the input file).
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Output HTML (interactive viewer)
    html_content = generate_html_report(report)
    html_path = reports_dir / f"multi-agent-report_{data['portfolio_name']}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"\n✓ JSON report written to {report_json_path}", file=sys.stderr)
    print(f"✓ HTML report written to {html_path}", file=sys.stderr)
    print(json.dumps(report, indent=2, ensure_ascii=False))


def consolidate_report(
    data: dict[str, Any],
    results: dict[str, str],
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine three agent outputs + meta-analyst recommendation into final report."""
    return {
        "portfolio_name": data["portfolio_name"],
        "timestamp": None,  # Would add datetime.now() here
        "data_freshness": freshness,
        "portfolio_snapshot": {
            "num_positions": data["metadata"]["num_positions"],
            "total_weight_pct": data["metadata"]["total_weight_pct"],
            "hhi": data["concentration"]["hhi"],
            "top_5_pct": data["concentration"]["top_5_pct"],
            "avg_volatility": data.get("risk_profile", {}).get("avg_volatility"),
            "portfolio_risk_pct": data.get("risk_profile", {}).get("portfolio_risk_pct"),
        },
        "top_5_by_risk": data.get("top_5_by_risk", []),
        "analyses": {
            "portfolio_structure": results.get("portfolio-analyst", ""),
            "market_and_fundamental": results.get("market-fundamental-analyst", ""),
            "risk_assessment": results.get("risk-analyst", ""),
        },
        # Present whenever agent_skills.md defines a rebalancing-analyst block
        # (always, in practice — it's mandatory). Kept conditional only as a
        # safety net for a malformed/missing block, not a feature flag.
        **({"rebalancing_recommendation": results["rebalancing-analyst"]} if "rebalancing-analyst" in results else {}),
        "meta_recommendation": results.get("meta-analyst", ""),
    }


def _render_markdown(text: str) -> str:
    """Agent output is Markdown (bold, occasional headers); render it to HTML
    instead of dropping it raw into a <p> tag, which just shows literal
    ** and ## characters in the browser."""
    return markdown_lib.markdown(text)


def generate_html_report(report: dict[str, Any]) -> str:
    """Generate interactive HTML report from consolidated JSON."""
    top5 = report.get("top_5_by_risk", [])
    analyses = report.get("analyses", {})
    meta_recommendation = report.get("meta_recommendation", "")
    rebalancing_recommendation = report.get("rebalancing_recommendation", "")
    freshness = report.get("data_freshness", {})
    snapshot = report.get("portfolio_snapshot", {})

    freshness_days = freshness.get("age_days", 0) if freshness else 0
    is_stale = freshness.get("is_stale", False) if freshness else False
    freshness_class = "stale" if is_stale else ""
    freshness_text = f"Stale ({freshness_days}d old, >30d)" if is_stale else f"Fresh ({freshness_days}d old)"

    # Pre-format snapshot values
    num_positions = snapshot.get("num_positions", "—")
    total_weight_pct = snapshot.get("total_weight_pct", "—")
    hhi = f"{snapshot.get('hhi', 0):.1f}" if isinstance(snapshot.get("hhi"), (int, float)) else "—"
    top5_pct = f"{snapshot.get('top_5_pct', 0):.2f}%" if isinstance(snapshot.get("top_5_pct"), (int, float)) else "—"

    top5_rows = "\n    ".join(
        f"""    <tr>
      <td>{i}</td>
      <td class="ticker">{row.get('ticker', '—')}</td>
      <td>{row.get('name', '')[:25]}</td>
      <td>{row.get('weight_pct', 0):.2f}%</td>
      <td>{row.get('volatility', 0):.1f}%</td>
      <td class="risk-high">{row.get('risk_contribution_pct', 0):.2f}%</td>
      <td>{row.get('beta', 0):.2f}</td>
    </tr>"""
        for i, row in enumerate(top5, 1)
    )

    # Rebalancing tab is present whenever the field is (i.e. always, in
    # practice — the agent is mandatory; see consolidate_report()).
    rebalancing_tab_button = (
        """  <button class="tab-button" onclick="switchTab(event, 'rebalancing')">
    🔄 Rebalancing
  </button>"""
        if rebalancing_recommendation else ""
    )
    rebalancing_tab_content = (
        f"""<div id="rebalancing" class="tab-content">
  <div class="analysis-text">
    {_render_markdown(rebalancing_recommendation)}
  </div>
</div>"""
        if rebalancing_recommendation else ""
    )

    html = f"""<meta charset="utf-8">
<title>Portfolio Analysis — {report.get('portfolio_name', 'portfolio')}</title>
<style>
  :root {{
    --bg-primary: #ffffff;
    --bg-secondary: #f5f5f5;
    --text-primary: #1a1a1a;
    --text-secondary: #666666;
    --border: #e0e0e0;
    --accent: #2563eb;
    --success: #16a34a;
    --warning: #ea580c;
  }}

  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg-primary: #1a1a1a;
      --bg-secondary: #2d2d2d;
      --text-primary: #ffffff;
      --text-secondary: #b0b0b0;
      --border: #3d3d3d;
    }}
  }}

  :root[data-theme="dark"] {{
    --bg-primary: #1a1a1a;
    --bg-secondary: #2d2d2d;
    --text-primary: #ffffff;
    --text-secondary: #b0b0b0;
    --border: #3d3d3d;
  }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    line-height: 1.6;
    color: var(--text-primary);
    background: var(--bg-primary);
    margin: 0;
    padding: 20px;
    max-width: 1000px;
    margin: 0 auto;
  }}

  h1 {{
    margin: 0 0 20px 0;
    font-size: 28px;
    font-weight: 600;
  }}

  h2 {{
    margin: 24px 0 16px 0;
    font-size: 18px;
    font-weight: 600;
  }}

  .freshness-banner {{
    background: linear-gradient(135deg, var(--success), #22c55e);
    color: white;
    padding: 16px;
    border-radius: 8px;
    margin-bottom: 24px;
    display: flex;
    align-items: center;
    gap: 12px;
  }}

  .freshness-banner.stale {{
    background: linear-gradient(135deg, var(--warning), #f97316);
  }}

  .freshness-banner strong {{
    font-weight: 600;
  }}

  .snapshot {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }}

  .stat-box {{
    background: var(--bg-secondary);
    padding: 16px;
    border-radius: 8px;
    border: 1px solid var(--border);
  }}

  .stat-label {{
    font-size: 12px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
  }}

  .stat-value {{
    font-size: 24px;
    font-weight: 700;
    color: var(--accent);
  }}

  .scope-note {{
    font-size: 13px;
    color: var(--text-secondary);
    margin: -12px 0 24px 0;
  }}

  .top5-table {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 24px;
    background: var(--bg-secondary);
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid var(--border);
  }}

  .top5-table th {{
    background: var(--accent);
    color: white;
    padding: 12px;
    text-align: left;
    font-weight: 600;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}

  .top5-table td {{
    padding: 12px;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
  }}

  .top5-table tr:last-child td {{
    border-bottom: none;
  }}

  .ticker {{
    font-weight: 600;
    color: var(--accent);
    font-family: 'Monaco', 'Courier New', monospace;
  }}

  .risk-high {{
    color: var(--warning);
    font-weight: 600;
  }}

  .tabs {{
    display: flex;
    gap: 0;
    border-bottom: 2px solid var(--border);
    margin-bottom: 24px;
    flex-wrap: wrap;
  }}

  .tab-button {{
    padding: 12px 20px;
    background: none;
    border: none;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
    color: var(--text-secondary);
    border-bottom: 3px solid transparent;
    transition: all 0.2s;
  }}

  .tab-button:hover {{
    color: var(--text-primary);
  }}

  .tab-button.active {{
    color: var(--accent);
    border-bottom-color: var(--accent);
  }}

  .tab-content {{
    display: none;
  }}

  .tab-content.active {{
    display: block;
  }}

  .analysis-text {{
    background: var(--bg-secondary);
    padding: 20px;
    border-radius: 8px;
    border: 1px solid var(--border);
    line-height: 1.8;
    font-size: 15px;
  }}

  .analysis-text p {{
    margin: 0 0 12px 0;
  }}

  .analysis-text p:last-child {{
    margin-bottom: 0;
  }}

  @media (max-width: 640px) {{
    body {{
      padding: 12px;
    }}

    h1 {{
      font-size: 20px;
    }}

    .snapshot {{
      grid-template-columns: 1fr 1fr;
    }}

    .top5-table {{
      font-size: 12px;
    }}

    .top5-table th, .top5-table td {{
      padding: 8px;
    }}

    .tab-button {{
      padding: 10px 12px;
      font-size: 12px;
    }}
  }}
</style>

<h1>Portfolio Analysis — 3-Agent Report</h1>

<div class="freshness-banner {freshness_class}">
  <span>{'✓' if not is_stale else '⚠'}</span>
  <strong>Data Status:</strong> {freshness_text}
  {'<span style="font-size: 12px; opacity: 0.9;">No automatic refresh — run the R pipeline to update prices.</span>' if is_stale else ''}
</div>

<div class="snapshot">
  <div class="stat-box">
    <div class="stat-label">Positions</div>
    <div class="stat-value">{num_positions}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">HHI (Concentration)</div>
    <div class="stat-value">{hhi}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Top 5 Weight</div>
    <div class="stat-value">{top5_pct}</div>
  </div>
</div>
<p class="scope-note">HHI and Top 5 Weight are computed on these {num_positions} analyzed
positions only ({total_weight_pct}% of the full portfolio) — not comparable to a
full-portfolio HHI computed elsewhere (e.g. the R pipeline's own report), which covers
every holding and uses a 0–1 scale rather than 0–10000.</p>

<h2>Top {len(top5)} by Risk Contribution</h2>
<table class="top5-table">
  <thead>
    <tr>
      <th>Rank</th>
      <th>Ticker</th>
      <th>Name</th>
      <th>Weight</th>
      <th>Volatility</th>
      <th>Risk %</th>
      <th>Beta</th>
    </tr>
  </thead>
  <tbody>
    {top5_rows}
  </tbody>
</table>

<h2>Agent Analyses</h2>

<div class="tabs">
  <button class="tab-button active" onclick="switchTab(event, 'recommendation')">
    💡 Recommendation
  </button>
  <button class="tab-button" onclick="switchTab(event, 'portfolio')">
    📊 Portfolio Structure
  </button>
  <button class="tab-button" onclick="switchTab(event, 'market')">
    📈 Market & Fundamental
  </button>
  <button class="tab-button" onclick="switchTab(event, 'risk')">
    ⚠️ Risk Assessment
  </button>
{rebalancing_tab_button}
</div>

<div id="recommendation" class="tab-content active">
  <div class="analysis-text">
    <p><strong>Chief Strategist Recommendation:</strong></p>
    {_render_markdown(meta_recommendation) if meta_recommendation else '<p>Generating recommendation...</p>'}
  </div>
</div>

<div id="portfolio" class="tab-content">
  <div class="analysis-text">
    {_render_markdown(analyses.get('portfolio_structure') or 'No analysis available.')}
  </div>
</div>

<div id="market" class="tab-content">
  <div class="analysis-text">
    {_render_markdown(analyses.get('market_and_fundamental') or 'No analysis available.')}
  </div>
</div>

<div id="risk" class="tab-content">
  <div class="analysis-text">
    {_render_markdown(analyses.get('risk_assessment') or 'No analysis available.')}
  </div>
</div>

{rebalancing_tab_content}

<script>
function switchTab(evt, tabName) {{
  const contents = document.querySelectorAll('.tab-content');
  contents.forEach(content => content.classList.remove('active'));

  const buttons = document.querySelectorAll('.tab-button');
  buttons.forEach(button => button.classList.remove('active'));

  document.getElementById(tabName).classList.add('active');
  evt.currentTarget.classList.add('active');
}}
</script>
"""
    return html


def main():
    """CLI entry point."""
    # Load .env file if present
    env_path = Path(__file__).parent.parent / "config" / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✓ Loaded .env from {env_path}", file=sys.stderr)
    else:
        load_dotenv()  # Load from parent directories or environment

    # Check API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        print("Set it in config/.env or export ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python supervisor.py <path_to_portfolio.xlsx> [portfolio_name]")
        sys.exit(1)

    excel_path = Path(sys.argv[1])
    portfolio_name = sys.argv[2] if len(sys.argv) > 2 else "portfolio"
    if not excel_path.exists():
        print(f"Error: {excel_path} not found", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run_supervisor(excel_path, portfolio_name))


if __name__ == "__main__":
    main()
