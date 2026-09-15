# data/

This directory holds runtime input for `supervisor.py`:

- `portfolio.xlsx` — your real portfolio snapshot (ticker, name, weight_pct,
  sector, volatility, risk_contribution_pct, market_beta). **Not committed**
  (gitignored) — bring your own, exported from your own portfolio tooling.
- `closing_prices.csv` / `volatility.csv` — price/volatility history from your
  own data pipeline. **Not committed** — see `*.example.csv` in this folder
  for the expected schema with synthetic sample data.

The `.example.csv` files are safe, synthetic data (fake tickers/prices) meant
only to let you test-run the pipeline end-to-end before wiring up your own
data source. Copy and rename them (dropping `.example`) to try a run, or
point `PRICE_SRC`/`CORRELATION_SRC` env vars in `pull-to-nas.sh` at your real
pipeline output.
