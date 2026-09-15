#!/bin/bash
# Deploy the multi-agent project *on the NAS* by pulling from the Synology
# Drive-synced working copy into the Docker project directory, then fixing
# ownership so the container's non-root user (uid 1000 / `agent`) can read
# the bind mounts.
#
# Run this ON THE NAS, as root (needed for chown). First time, run it from
# the Synology Drive-synced copy; it copies itself into $DEST for later:
#   ssh nasuser@nas.local
#   sudo /volume2/homes/nasuser/Drive/your-repo/multi-agent/pull-to-nas.sh --build
# After that:
#   sudo /volume1/docker/portfolio-multi-agent/pull-to-nas.sh          # sync only
#   sudo /volume1/docker/portfolio-multi-agent/pull-to-nas.sh --build  # sync + rebuild image
#
# Why pull instead of scp-push from the Mac: pushed files landed owned by
# the SSH user, under home-folder ACLs the container could not traverse, so
# the container hit "Permission denied" on the mounts. Copying locally on
# the NAS as root and chown-ing to the container uid sidesteps that.
# See CLAUDE.md ("Synology DSM bind-mount write permission fix").
#
# NOTE: Synology Drive does not sync dotfiles, so nothing this script needs
# from $SRC may be named with a leading "." (that is why the env template is
# config/env.example, not config/.env.example). The real config/.env is
# created directly on the NAS and never synced or copied.

set -euo pipefail

# --- Config (override via environment) -------------------------------------
# Synology Drive-synced working copy of this repo on the NAS:
SRC="${SRC:-/volume2/homes/nasuser/Drive/your-repo/multi-agent}"
# Docker project directory (the bind-mount root):
DEST="${DEST:-/volume1/docker/portfolio-multi-agent}"
# R pipeline output (closing_prices.csv / volatility.csv):
PRICE_SRC="${PRICE_SRC:-/volume2/homes/nasuser/Drive/your-repo/portfolio/meta}"
# R pipeline output (correlation_matrix_<portfolio>.csv per portfolio id,
# e.g. correlation_matrix_example.csv, exported directly by
# portfolio_report.Rmd's correlation-heatmap chunk). Optional — supervisor.py
# skips it gracefully (mtime > 30d) rather than trusting stale correlations.
CORRELATION_SRC="${CORRELATION_SRC:-/volume2/homes/nasuser/Drive/your-repo/portfolio/data/clean}"
# UID/GID of the non-root `agent` user baked into the image:
CONTAINER_UID="${CONTAINER_UID:-1000}"

DO_BUILD=0
[ "${1:-}" = "--build" ] && DO_BUILD=1

# --- Preflight ------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  echo "✗ Run as root (chown to uid $CONTAINER_UID needs it):  sudo $0" >&2
  exit 1
fi
if [ ! -d "$SRC" ]; then
  echo "✗ Source working copy not found: $SRC" >&2
  echo "  Set SRC=/path/to/synced/multi-agent and re-run." >&2
  exit 1
fi

echo "Pulling  $SRC"
echo "     ->  $DEST"
mkdir -p "$DEST"/{app,config,data,reports}

# --- Code ---------------------------------------------------------------
cp -f "$SRC"/app/*.py "$SRC"/app/*.md "$DEST/app/"
echo "✓ app/*.py + app/*.md"

cp -f "$SRC/Dockerfile" "$SRC/docker-compose.yml" "$SRC/requirements.txt" "$DEST/"
[ -f "$SRC/README.md" ] && cp -f "$SRC/README.md" "$DEST/"
# Copy this script too, so later runs can use $DEST/pull-to-nas.sh. Atomic
# mv, and skipped when SRC already is DEST — safe to overwrite even while
# running from $DEST/pull-to-nas.sh.
if [ -f "$SRC/pull-to-nas.sh" ] && ! [ "$SRC/pull-to-nas.sh" -ef "$DEST/pull-to-nas.sh" ]; then
  cp -f "$SRC/pull-to-nas.sh" "$DEST/.pull-to-nas.sh.tmp"
  chmod +x "$DEST/.pull-to-nas.sh.tmp"
  mv -f "$DEST/.pull-to-nas.sh.tmp" "$DEST/pull-to-nas.sh"
fi
echo "✓ Dockerfile, docker-compose.yml, requirements.txt, README.md, pull-to-nas.sh"

# --- Config (never clobber a real .env) -------------------------------
if [ -f "$SRC/config/env.example" ]; then
  cp -f "$SRC/config/env.example" "$DEST/config/"
  echo "✓ config/env.example"
else
  echo "⚠ $SRC/config/env.example not found — skipped (template only)" >&2
fi
if [ ! -f "$DEST/config/.env" ]; then
  echo "⚠ $DEST/config/.env missing — cp config/env.example config/.env and set ANTHROPIC_API_KEY" >&2
else
  echo "✓ kept existing config/.env"
fi

# --- Data: portfolio xlsx + price CSVs --------------------------------
if [ -f "$SRC/data/portfolio.xlsx" ]; then
  cp -f "$SRC/data/portfolio.xlsx" "$DEST/data/"
  echo "✓ data/portfolio.xlsx"
else
  echo "⚠ $SRC/data/portfolio.xlsx not found — skipped" >&2
fi
for f in closing_prices.csv volatility.csv; do
  if [ -f "$PRICE_SRC/$f" ]; then
    cp -f "$PRICE_SRC/$f" "$DEST/data/"
    echo "✓ data/$f (from $PRICE_SRC)"
  else
    echo "⚠ $PRICE_SRC/$f not found — run_weekly_analysis.sh refreshes it before each run" >&2
  fi
done
correlation_copied=0
for f in "$CORRELATION_SRC"/correlation_matrix_*.csv; do
  [ -e "$f" ] || continue
  cp -f "$f" "$DEST/data/"
  echo "✓ data/$(basename "$f") (from $CORRELATION_SRC)"
  correlation_copied=1
done
if [ "$correlation_copied" -eq 0 ]; then
  echo "⚠ no correlation_matrix_*.csv found in $CORRELATION_SRC — optional, supervisor.py runs fine without it" >&2
fi

# --- Ownership: match the container's non-root user -----------------
# DSM shared-folder ACLs override POSIX mode bits; the container (uid
# $CONTAINER_UID) only gets read/write on the bind mounts if it OWNS the
# paths. chmod alone does nothing here.
chown -R "$CONTAINER_UID:$CONTAINER_UID" "$DEST"
echo "✓ chown -R $CONTAINER_UID:$CONTAINER_UID $DEST"

# --- Optional rebuild ---------------------------------------------
if [ "$DO_BUILD" -eq 1 ]; then
  echo "Building image (docker build --network=host)..."
  ( cd "$DEST" && docker build --network=host -t portfolio-multi-agent:latest . )
  echo "✓ image rebuilt: portfolio-multi-agent:latest"
fi

echo
echo "✓ Sync complete."
if [ "$DO_BUILD" -eq 0 ]; then
  echo "  Rebuild image:  cd $DEST && sudo docker build --network=host -t portfolio-multi-agent:latest ."
fi
echo "  Weekly wrapper: $DEST/run_weekly_analysis.sh  (DSM Task Scheduler, runs as root)"
