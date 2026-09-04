#!/usr/bin/env bash
# Build the local Odoo 17 development target from nothing.
#
#   docker/seed.sh
#
# Odoo UI  http://localhost:8069   (db erp_planner, admin/admin)
# Postgres postgresql://odoo:odoo@localhost:5433/erp_planner
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker/docker-compose.yml"

wait_for_odoo() {
  for _ in $(seq 1 90); do
    curl -sf -o /dev/null http://localhost:8069/web/login && return 0
    sleep 1
  done
  echo "odoo did not come up" >&2; exit 1
}

echo "==> starting containers"
$COMPOSE up -d
wait_for_odoo

echo "==> creating database with demo data (several minutes)"
# `exec` bypasses the image entrypoint, which is what normally turns HOST/USER/PASSWORD into
# --db_* flags, so they have to be passed explicitly here.
$COMPOSE exec -T odoo odoo -d erp_planner \
  -i base,sale_management,purchase,stock,mrp,account,hr \
  --db_host=db --db_user=odoo --db_password=odoo \
  --stop-after-init --log-level=warn || true

echo "==> restarting odoo against the new database"
$COMPOSE restart odoo
wait_for_odoo

echo "==> adding Tier 2 customisations"
# Run twice on purpose: a manual model's access rules are not visible to the running registry
# until it reloads, so the first pass creates the models and the restart makes them usable.
python docker/seed_custom_models.py || true
$COMPOSE restart odoo
wait_for_odoo
python docker/seed_custom_models.py

echo "==> analyzing (ingestion reads pg_stats, which ANALYZE populates)"
$COMPOSE exec -T db psql -U odoo -d erp_planner -c "ANALYZE;" >/dev/null

echo "done."
