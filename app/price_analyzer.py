"""
Analyze historical price data from closing_prices.csv.

Computes per-ticker: volatility, drawdown, current price, momentum.
Used by agents for risk analysis.
"""

import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

STALE_THRESHOLD_DAYS = 30


class PriceAnalyzer:
    """Analyze historical prices for portfolio tickers."""

    def __init__(self, prices_csv: Path, volatility_csv: Optional[Path] = None):
        """
        Load price data from CSV.

        Args:
            prices_csv: closing_prices.csv path
            volatility_csv: volatility.csv path (optional, for pre-calculated vols)
        """
        # Load prices
        self.df = pd.read_csv(prices_csv)
        self.df["Date"] = pd.to_datetime(self.df["Date"])
        self.df = self.df.sort_values("Date")

        # Group by ticker
        self.by_ticker = {
            ticker: group.sort_values("Date")
            for ticker, group in self.df.groupby("ticker")
        }

        print(f"✓ Loaded {len(self.by_ticker)} tickers, {len(self.df)} total records")

        # Load pre-calculated volatilities if provided
        self.volatility_data = {}
        if volatility_csv and volatility_csv.exists():
            vol_df = pd.read_csv(volatility_csv)
            self.volatility_data = {
                row["ticker"]: {
                    "est_vol_pct": row.get("est_vol_pct"),
                    "hist_vol_pct": row.get("hist_vol_pct"),
                    "vol_source": row.get("vol_source", "Unknown"),
                    "company": row.get("company"),
                    "sector": row.get("sector"),
                }
                for _, row in vol_df.iterrows()
                if pd.notna(row.get("ticker"))
            }
            print(f"✓ Loaded volatility data for {len(self.volatility_data)} tickers")

    def get_data_freshness(self, asof: Optional[datetime] = None) -> dict:
        """
        Check how old the newest price in closing_prices.csv is.

        This is a pure CSV check — it never calls an external API. The
        supervisor runs it first on every analysis; if the data is stale the
        agents are told to flag it in the report rather than refreshing it.

        Returns:
            {
              "latest_date": "YYYY-MM-DD",
              "age_days": int,
              "is_stale": bool,
              "threshold_days": int,
              "num_tickers": int,
              "warning": str or None,
            }
        """
        asof = asof or datetime.now()
        latest = self.df["Date"].max()
        age_days = (asof - latest).days

        is_stale = age_days > STALE_THRESHOLD_DAYS
        warning = None
        if is_stale:
            warning = (
                f"Price data in closing_prices.csv is {age_days} days old "
                f"(latest: {latest.strftime('%Y-%m-%d')}, threshold: "
                f"{STALE_THRESHOLD_DAYS} days). Prices, volatilities, drawdowns "
                f"and momentum below may not reflect current market conditions. "
                f"No automatic EODHD refresh is performed — re-run the R price "
                f"pipeline (scripts/get_price_data.R) to update."
            )

        return {
            "latest_date": latest.strftime("%Y-%m-%d"),
            "age_days": age_days,
            "is_stale": is_stale,
            "threshold_days": STALE_THRESHOLD_DAYS,
            "num_tickers": len(self.by_ticker),
            "warning": warning,
        }

    def get_ticker_prices(self, ticker: str) -> Optional[pd.DataFrame]:
        """Get all price records for a ticker."""
        return self.by_ticker.get(ticker)

    def _validate_ticker_data(self, ticker: str, min_rows: int = 2) -> tuple:
        """
        Validate ticker prices exist and have minimum rows.

        Returns: (prices_df, error_dict)
        - If valid: (prices_df, None)
        - If invalid: (None, {"[metric]_pct": None, "source": None, "error": "message"})
        """
        prices = self.get_ticker_prices(ticker)
        if prices is None or len(prices) < min_rows:
            error_dict = {
                "[metric]_pct": None,
                "source": None,
                "error": f"No data (need {min_rows}, got {len(prices) if prices is not None else 0})",
            }
            return None, error_dict
        return prices, None

    def _extract_closes(self, prices: pd.DataFrame, lookback: Optional[int] = None) -> list:
        """
        Extract Adjusted_close column, optionally with lookback window.

        Args:
            prices: DataFrame with Adjusted_close column
            lookback: If set, return last N rows; if None, return all rows

        Returns: numpy array of closing prices
        """
        if lookback:
            prices = prices.tail(lookback)
        return prices["Adjusted_close"].values

    def calculate_volatility(self, ticker: str, days: int = 252) -> dict:
        """
        Get volatility: prefer pre-calculated, fallback to computed from prices.

        Uses true log returns: ln(P_t / P_{t-1}), annualized via √252 trading days.

        Args:
            ticker: Symbol
            days: Look-back period (for computed, default 252 = 1 year)

        Returns:
            {"volatility_pct": float, "source": str, "error": str or null}
        """
        # Check if pre-calculated volatility exists
        if ticker in self.volatility_data:
            vol_info = self.volatility_data[ticker]
            # Prefer historical if available
            vol = vol_info.get("hist_vol_pct")
            if vol is None:
                vol = vol_info.get("est_vol_pct")
            source = vol_info.get("vol_source", "Unknown")

            if vol is not None:
                return {
                    "volatility_pct": float(vol),
                    "source": source,
                    "error": None,
                }

        # Fallback: compute from prices using log returns
        prices, error = self._validate_ticker_data(ticker, min_rows=2)
        if error:
            return error

        closes = self._extract_closes(prices, lookback=days)

        if len(closes) < 2:
            return {
                "volatility_pct": None,
                "source": None,
                "error": "Insufficient data",
            }

        # True log returns: ln(P_t / P_{t-1})
        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]

        if not log_returns:
            return {
                "volatility_pct": None,
                "source": None,
                "error": "Cannot compute",
            }

        # Annualized volatility: daily_std × √252 × 100
        daily_vol = statistics.stdev(log_returns)
        annual_vol = daily_vol * (252 ** 0.5) * 100

        return {
            "volatility_pct": round(annual_vol, 2),
            "source": "Computed",
            "error": None,
        }

    def calculate_drawdown(self, ticker: str) -> dict:
        """
        Calculate maximum drawdown from all-time peak.

        Returns:
            {"max_drawdown_pct": float, "error": str or null}
        """
        prices, error = self._validate_ticker_data(ticker, min_rows=1)
        if error:
            error["max_drawdown_pct"] = error.pop("[metric]_pct", None)
            return error

        closes = self._extract_closes(prices)
        if len(closes) < 2:
            return {"max_drawdown_pct": None, "error": "Insufficient data"}

        cummax = max(closes)
        dd = ((cummax - min(closes)) / cummax) * 100

        return {"max_drawdown_pct": round(dd, 2), "error": None}

    def calculate_current_drawdown(self, ticker: str) -> dict:
        """
        Calculate current drawdown from recent peak (not all-time).

        Useful for active monitoring: how far below the recent high are we now?

        Returns:
            {"current_drawdown_pct": float, "peak_date": str, "error": str or null}
        """
        prices, error = self._validate_ticker_data(ticker, min_rows=1)
        if error:
            error["current_drawdown_pct"] = error.pop("[metric]_pct", None)
            error["peak_date"] = None
            return error

        closes = self._extract_closes(prices)
        if len(closes) < 1:
            return {"current_drawdown_pct": None, "peak_date": None, "error": "Insufficient data"}

        # Find the most recent peak
        running_max = 0
        peak_idx = 0
        for i, close in enumerate(closes):
            if close > running_max:
                running_max = close
                peak_idx = i

        current = closes[-1]
        if running_max == 0:
            return {"current_drawdown_pct": None, "peak_date": None, "error": "Invalid data"}

        dd = ((running_max - current) / running_max) * 100
        peak_date = prices.iloc[peak_idx]["Date"].strftime("%Y-%m-%d")

        return {
            "current_drawdown_pct": round(dd, 2),
            "peak_date": peak_date,
            "error": None,
        }

    def get_current_price(self, ticker: str) -> dict:
        """
        Get the most recent price (current or latest in CSV).

        Returns:
            {"price": float, "date": str, "error": str or null}
        """
        prices, error = self._validate_ticker_data(ticker, min_rows=1)
        if error:
            error["price"] = error.pop("[metric]_pct", None)
            error["date"] = None
            return error

        latest = prices.iloc[-1]
        return {
            "price": round(float(latest["Adjusted_close"]), 2),
            "date": latest["Date"].strftime("%Y-%m-%d"),
            "error": None,
        }

    def get_momentum(self, ticker: str, periods: int = 20) -> dict:
        """
        Calculate price momentum (% change over last N days).

        Returns:
            {"momentum_pct": float, "error": str or null}
        """
        prices, error = self._validate_ticker_data(ticker, min_rows=periods)
        if error:
            error["momentum_pct"] = error.pop("[metric]_pct", None)
            return error

        closes = self._extract_closes(prices, lookback=periods)

        if len(closes) < 2 or closes[0] == 0:
            return {"momentum_pct": None, "error": "Invalid data"}

        momentum = ((closes[-1] - closes[0]) / closes[0]) * 100
        return {"momentum_pct": round(momentum, 2), "error": None}

    def analyze_position(self, ticker: str) -> dict:
        """
        Comprehensive analysis for one position.

        Returns:
            {"ticker": ..., "volatility": ..., "drawdown": ..., "current_drawdown": ..., "current": ..., "momentum": ...}
        """
        return {
            "ticker": ticker,
            "volatility": self.calculate_volatility(ticker),
            "drawdown": self.calculate_drawdown(ticker),
            "current_drawdown": self.calculate_current_drawdown(ticker),
            "current": self.get_current_price(ticker),
            "momentum": self.get_momentum(ticker),
        }

    def analyze_multiple(self, tickers: list[str]) -> list[dict]:
        """Analyze multiple tickers in parallel."""
        return [self.analyze_position(ticker) for ticker in tickers if ticker in self.by_ticker]


def main():
    """CLI test."""
    prices_csv = Path("/path/to/your/R-pipeline/output/closing_prices.csv")
    volatility_csv = Path("/path/to/your/R-pipeline/output/volatility.csv")

    analyzer = PriceAnalyzer(prices_csv, volatility_csv)

    fresh = analyzer.get_data_freshness()
    print(f"\nData freshness: latest {fresh['latest_date']}, {fresh['age_days']}d old, "
          f"stale={fresh['is_stale']}")
    if fresh["warning"]:
        print(f"  ⚠ {fresh['warning']}")

    # Test with a few tickers from the portfolio
    test_tickers = ["MSFT", "V", "2330.TW", "005930.KO", "000660.KO"]

    for ticker in test_tickers:
        analysis = analyzer.analyze_position(ticker)
        vol_info = analysis['volatility']
        source = vol_info.get('source', '?')
        dd_info = analysis['drawdown']
        curr_dd = analysis['current_drawdown']
        print(f"\n{ticker}:")
        print(f"  Vol: {vol_info['volatility_pct']}% ({source})")
        print(f"  Max DD (all-time): {dd_info['max_drawdown_pct']}%")
        print(f"  Current DD (from peak): {curr_dd['current_drawdown_pct']}% (peak: {curr_dd['peak_date']})")
        print(f"  Price: ${analysis['current']['price']} ({analysis['current']['date']})")
        print(f"  Momentum (20d): {analysis['momentum']['momentum_pct']}%")


if __name__ == "__main__":
    main()
