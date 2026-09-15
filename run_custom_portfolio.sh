#!/bin/sh
# run_custom_portfolio.sh — run the multi-agent analysis against a portfolio
# xlsx file living anywhere on the NAS, without touching portfolio.xlsx or
# the scheduled weekly pipeline (run_weekly_analysis.sh).
#
# Refreshes price/volatility data (same sync_price_data.sh the weekly
# pipeline uses) before the custom analysis, so a custom run never analyzes
# stale prices. Safe to do: the sync also touches data/portfolio.xlsx (the
# SCHEDULED portfolio), which is a different file from
# data/${PORTFOLIO_NAME}.xlsx below and is copied into place AFTER the sync
# anyway, so it's never at risk of being clobbered. supervisor.py now reads
# portfolio.xlsx directly (no more analysis_input_*.json intermediate), so
# there's nothing else to regenerate here.
# The sync step is best-effort and non-fatal — only failures in the custom
# portfolio's own steps (2+) abort the run.
#
# Run this ON THE NAS, as root (same user/context as run_weekly_analysis.sh),
# straight from the Drive-synced copy of this repo — unlike pull-to-nas.sh,
# nothing copies this script into /volume1/docker/portfolio-multi-agent/, so
# that path won't exist unless you put it there yourself:
#   sudo sh /volume2/homes/nasuser/Drive/your-repo/multi-agent/run_custom_portfolio.sh

# ---- CONFIG: edit these before running ---------------------------------
SOURCE_DIR="/volume1/homes/nasuser/Drive/your-repo/portfolio/data/output/"    # where the xlsx currently lives
SOURCE_FILE="partner_portfolio.xlsx"                # its filename
PORTFOLIO_NAME="partner"                      # used to name every output file
# --------------------------------------------------------------------------

BASE="/volume1/docker/portfolio-multi-agent"
DATA_DIR="$BASE/data"
REPORTS_DIR="$BASE/reports"
ENV_FILE="$BASE/config/.env"
LOG="$BASE/run_custom_portfolio.log"
DRIVE_DEST="/volume2/homes/nasuser/Drive/your-repo/multi-agent/reports"
SYNC_SCRIPT="$BASE/sync_price_data.sh"

SOURCE_XLSX="$SOURCE_DIR/$SOURCE_FILE"
DEST_XLSX="$DATA_DIR/${PORTFOLIO_NAME}.xlsx"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"; }

log "=== Starting custom portfolio run: $PORTFOLIO_NAME ($SOURCE_XLSX) ==="

if [ ! -f "$SOURCE_XLSX" ]; then
  log "ERROR: source file not found at $SOURCE_XLSX"
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  log "ERROR: $ENV_FILE not found, aborting"
  exit 1
fi
ANTHROPIC_API_KEY=$(grep '^ANTHROPIC_API_KEY=' "$ENV_FILE" | cut -d'=' -f2-)
if [ -z "$ANTHROPIC_API_KEY" ]; then
  log "ERROR: ANTHROPIC_API_KEY not set in $ENV_FILE, aborting"
  exit 1
fi

# 1. Refresh price/volatility data (best-effort: a stale sync shouldn't block
#    a one-off run when data/ already has last week's CSVs to fall back on).
if [ -x "$SYNC_SCRIPT" ]; then
  if "$SYNC_SCRIPT" >> "$LOG" 2>&1; then
    log "OK - ran $SYNC_SCRIPT (price/volatility data refreshed)"
  else
    log "WARNING: $SYNC_SCRIPT exited with an error - continuing with existing data/ contents"
  fi
  chown -R 1000:1000 "$DATA_DIR"
else
  log "WARNING: $SYNC_SCRIPT not found or not executable - skipping price/volatility refresh, using existing data/ contents"
fi

# 2. Copy the source file into the container's data mount, named after PORTFOLIO_NAME
#    (after the sync above, so it can't be overwritten by it)
cp -f "$SOURCE_XLSX" "$DEST_XLSX"
chown 1000:1000 "$DEST_XLSX"
log "OK - copied $SOURCE_XLSX -> $DEST_XLSX"

# 3. Run the analysis container directly against that xlsx
docker run --rm --network host -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -v "$DATA_DIR:/data:ro" -v "$REPORTS_DIR:/reports" \
  portfolio-multi-agent:latest /app/supervisor.py "/data/${PORTFOLIO_NAME}.xlsx" "$PORTFOLIO_NAME" >> "$LOG" 2>&1

if [ $? -eq 0 ]; then
  log "OK - analysis complete: reports/multi-agent-report_${PORTFOLIO_NAME}.{json,html}"

  # 4. Copy the report into the Synology Drive-synced folder (reaches the Mac automatically)
  mkdir -p "$DRIVE_DEST"
  cp -f "$REPORTS_DIR/multi-agent-report_${PORTFOLIO_NAME}.json" \
        "$REPORTS_DIR/multi-agent-report_${PORTFOLIO_NAME}.html" \
        "$DRIVE_DEST/" 2>>"$LOG"
  if [ $? -eq 0 ]; then
    chown --reference="$(dirname "$DRIVE_DEST")" \
      "$DRIVE_DEST" \
      "$DRIVE_DEST/multi-agent-report_${PORTFOLIO_NAME}.json" \
      "$DRIVE_DEST/multi-agent-report_${PORTFOLIO_NAME}.html"
    log "OK - report copied to Drive-synced folder"
  else
    log "WARNING: failed to copy report into Drive-synced folder - report still in $REPORTS_DIR/"
  fi
else
  log "FAILED - analysis run exited with error, see log above for details"
fi

log "=== Finished custom portfolio run: $PORTFOLIO_NAME ==="
