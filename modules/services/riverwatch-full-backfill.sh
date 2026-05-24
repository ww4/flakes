#!/usr/bin/env bash
# Full historical Riverwatch backfill orchestrator.
#
# Runs 5 USGS fetch passes covering:
#   - LPTK2 flow DV 1925-10-01 → 1989-09-30  (decades of daily values)
#   - LPTK2 flow UV 1989-10-01 → present     (15-min)
#   - LPTK2 stage UV 2007-10-01 → present    (15-min; also computes flood_category)
#   - GSTK2 stage UV 2009-10-01 → present
#   - GSTK2 flow UV 2012-09-01 → present
#
# Then creates Prometheus TSDB blocks via promtool and installs them into
# /var/lib/prometheus2/data. Wipes the data dir first for a clean state.
set -uo pipefail   # don't -e — one failed USGS chunk shouldn't abort everything

OM_DIR=/tmp/rw
BLOCKS_DIR=/tmp/rw/blocks
SCRIPT=/tmp/riverwatch-backfill.py

echo "=== stop prom + wipe ==="
sudo systemctl stop prometheus
sudo rm -rf /var/lib/prometheus2/data/* "$OM_DIR"
sudo install -d -o prometheus -g prometheus /var/lib/prometheus2/data
mkdir -p "$OM_DIR" "$BLOCKS_DIR"

echo
echo "=== fetch all 5 datasets ==="
date -Iseconds
nix-shell -p 'python3.withPackages(p:[p.httpx])' --run "
  $SCRIPT --gauge LPTK2 --start 1925-10-01 --end 1989-09-30 --freq dv --params 00060            --chunk-days 3650 --out $OM_DIR/lptk2-dv-flow.om
  $SCRIPT --gauge LPTK2 --start 1989-10-01 --end 2026-05-24 --freq iv --params 00060            --chunk-days 90   --out $OM_DIR/lptk2-uv-flow.om
  $SCRIPT --gauge LPTK2 --start 2007-10-01 --end 2026-05-24 --freq iv --params 00065            --chunk-days 90   --out $OM_DIR/lptk2-uv-stage.om
  $SCRIPT --gauge GSTK2 --start 2009-10-01 --end 2026-05-24 --freq iv --params 00065            --chunk-days 90   --out $OM_DIR/gstk2-uv-stage.om
  $SCRIPT --gauge GSTK2 --start 2012-09-01 --end 2026-05-24 --freq iv --params 00060            --chunk-days 90   --out $OM_DIR/gstk2-uv-flow.om
"
date -Iseconds
echo
echo "=== OpenMetrics file sizes ==="
ls -lh "$OM_DIR"/*.om

echo
echo "=== create blocks (one promtool pass per file) ==="
for f in "$OM_DIR"/*.om ; do
  echo "--- promtool: $f ---"
  nix-shell -p 'prometheus.cli' --run "promtool tsdb create-blocks-from openmetrics --max-block-duration=720h '$f' '$BLOCKS_DIR'" 2>&1 | tail -3
done

echo
echo "=== install blocks ==="
sudo cp -a "$BLOCKS_DIR"/01* /var/lib/prometheus2/data/
sudo chown -R prometheus:prometheus /var/lib/prometheus2/data/
echo "block count: $(ls /var/lib/prometheus2/data/ | grep -c '^01')"

echo
echo "=== start prometheus ==="
sudo systemctl start prometheus
sleep 12
systemctl is-active prometheus

echo
echo "=== done ==="
date -Iseconds
