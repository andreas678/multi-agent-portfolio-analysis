"""
EODHD REST API client for portfolio analysis.

Slim variant: historical EOD prices only (no fundamentals/news).
Used by agents to calculate realized volatility, drawdowns, momentum.
"""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import httpx

# Load .env if present
env_path = Path(__file__).parent.parent / "config" / ".env"
if env_path.exists():
    load_dotenv(env_path)


class EODHDClient:
    """EODHD REST API wrapper with rate-limiting."""

    BASE_URL = "https://eodhd.com/api"
    MIN_INTERVAL_S = 0.6  # Rate limit: ~1 call/sec

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("EODHD_API_KEY", "")
        if not self.api_key:
            raise ValueError("EODHD_API_KEY not set")

        self.client = httpx.Client(timeout=30.0)
        self._last_call_at = 0.0

    def _throttle(self):
        """Enforce rate limit."""
        elapsed = time.time() - self._last_call_at
        if elapsed < self.MIN_INTERVAL_S:
            time.sleep(self.MIN_INTERVAL_S - elapsed)
        self._last_call_at = time.time()

    def _request(self, endpoint: str, params: dict) -> dict:
        """Make rate-limited API request."""
        self._throttle()
        url = f"{self.BASE_URL}/{endpoint}"
        params["api_token"] = self.api_key

        try:
            resp = self.client.get(url, params=params)
            resp.raise_for_status()

            # Check if response is HTML error (EODHD returns HTML for plan limits)
            text = resp.text.strip()
            if "Value" in text and "subscription" in text.lower():
                # This is an HTML error like "Value\n Data is limited by one year..."
                msg = text.split("\n", 1)[1].strip() if "\n" in text else text
                return {"error": f"EODHD: {msg}"}

            try:
                return resp.json()
            except Exception as json_err:
                return {
                    "error": f"JSON parse: {json_err} (got: {text[:100]})"
                }
        except httpx.HTTPError as e:
            return {"error": f"HTTP {e}"}

    def get_eod_prices(
        self,
        ticker: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> dict:
        """
        Fetch EOD historical prices (max 1 year for free plan).

        Args:
            ticker: Symbol (e.g., 'AAPL', '2330.TW', '005930.KO')
            from_date: YYYY-MM-DD (default: max 1 year ago)
            to_date: YYYY-MM-DD (default: today)

        Returns:
            {"prices": [...], "error": null} or {"error": "..."}
        """
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if not from_date:
            # Free plan: max 365 days
            from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        params = {
            "period": "d",
            "fmt": "json",
            "from": from_date,
            "to": to_date,
        }

        result = self._request(f"eod/{ticker}", params)

        if "error" in result:
            return {"prices": [], "error": result["error"]}

        # Parse response: result is a list of {date, close, high, low, open, volume, ...}
        if isinstance(result, list):
            return {
                "prices": [
                    {
                        "date": p.get("date"),
                        "close": p.get("close"),
                        "high": p.get("high"),
                        "low": p.get("low"),
                        "volume": p.get("volume"),
                    }
                    for p in result
                ],
                "error": None,
            }

        return {"prices": [], "error": "Unexpected response format"}

    def get_current_price(self, ticker: str) -> dict:
        """
        Fetch the most recent EOD close.

        Returns: {"price": float, "date": str, "error": null} or {"error": "..."}
        """
        result = self._request(f"eod/{ticker}", {"period": "d"})

        if "error" in result:
            return {"price": None, "date": None, "error": result["error"]}

        if isinstance(result, list) and len(result) > 0:
            latest = result[0]
            return {
                "price": latest.get("close"),
                "date": latest.get("date"),
                "error": None,
            }

        return {"price": None, "date": None, "error": "No data"}


def calculate_volatility(prices: list[dict]) -> dict:
    """
    Calculate realized volatility from price history.

    Returns: {"volatility_pct": float, "num_days": int, "error": null}
    """
    if not prices or len(prices) < 2:
        return {"volatility_pct": None, "num_days": 0, "error": "Insufficient data"}

    closes = [p.get("close") for p in prices if p.get("close")]
    if len(closes) < 2:
        return {"volatility_pct": None, "num_days": len(closes), "error": "No closes"}

    # Log returns
    log_returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] != 0
    ]

    if not log_returns:
        return {
            "volatility_pct": None,
            "num_days": len(closes),
            "error": "Cannot compute returns",
        }

    # Annualized volatility: std(daily) * sqrt(252)
    import statistics

    daily_vol = statistics.stdev(log_returns)
    annual_vol = daily_vol * (252 ** 0.5) * 100  # Convert to %

    return {
        "volatility_pct": round(annual_vol, 2),
        "num_days": len(closes),
        "error": None,
    }


def calculate_drawdown(prices: list[dict]) -> dict:
    """
    Calculate maximum drawdown from peak.

    Returns: {"max_drawdown_pct": float, "error": null}
    """
    closes = [p.get("close") for p in prices if p.get("close")]
    if not closes:
        return {"max_drawdown_pct": None, "error": "No prices"}

    max_price = max(closes)
    min_price = min(closes)

    if max_price == 0:
        return {"max_drawdown_pct": None, "error": "Invalid data"}

    max_dd = ((max_price - min_price) / max_price) * 100
    return {"max_drawdown_pct": round(max_dd, 2), "error": None}


def main():
    """CLI test."""
    client = EODHDClient()

    ticker = "AAPL"
    print(f"Fetching {ticker} prices...")

    result = client.get_eod_prices(ticker, from_date="2023-01-01")
    if result["error"]:
        print(f"Error: {result['error']}")
        return

    prices = result["prices"]
    print(f"✓ Fetched {len(prices)} days of data")

    if prices:
        vol = calculate_volatility(prices)
        dd = calculate_drawdown(prices)
        print(f"  Volatility: {vol['volatility_pct']}%")
        print(f"  Max Drawdown: {dd['max_drawdown_pct']}%")

        current = client.get_current_price(ticker)
        print(f"  Current: ${current['price']} ({current['date']})")


if __name__ == "__main__":
    main()
