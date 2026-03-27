#!/usr/bin/env bash
# Provision OpenObserve dashboards and alerts for Theo.
# Idempotent — safe to run repeatedly. Creates or updates as needed.
#
# Usage: ./infra/provision.sh
# Override defaults: OPENOBSERVE_URL=... OPENOBSERVE_AUTH=... ./infra/provision.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
O2_URL="${OPENOBSERVE_URL:-http://localhost:5080}"
O2_ORG="default"
O2_AUTH="${OPENOBSERVE_AUTH:-Basic dGhlb0B0aGVvLmRldjp0aGVv}"

api() {
    local method="$1" path="$2"
    shift 2
    curl -sf -X "$method" \
        -H "Authorization: $O2_AUTH" \
        -H "Content-Type: application/json" \
        "${O2_URL}/api/${O2_ORG}${path}" "$@" 2>/dev/null
}

# --------------------------------------------------------------------------- #
# Wait for OpenObserve to be reachable
# --------------------------------------------------------------------------- #
echo "Waiting for OpenObserve at ${O2_URL} ..."
for i in $(seq 1 15); do
    if curl -sf -o /dev/null -H "Authorization: $O2_AUTH" "${O2_URL}/api/${O2_ORG}/summary" 2>/dev/null; then
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo "ERROR: OpenObserve not reachable after 15 seconds." >&2
        exit 1
    fi
    sleep 1
done

# --------------------------------------------------------------------------- #
# Dashboards
# --------------------------------------------------------------------------- #
echo ""
echo "Dashboards"
echo "----------"

# Fetch existing dashboards once
EXISTING_DASHBOARDS=$(api GET "/dashboards" || echo '{"dashboards":[]}')

provision_dashboard() {
    local file="$1"
    local title
    title=$(python3 -c "import sys,json; print(json.load(sys.stdin)['title'])" < "$file")

    # Check if dashboard with this title already exists
    local existing
    existing=$(python3 -c "
import sys, json
data = json.load(sys.stdin)
for d in data.get('dashboards', []):
    v = d.get('v5') or d.get('v4') or d.get('v3') or {}
    if v.get('title') == '$title':
        print(json.dumps({'id': v['dashboardId'], 'hash': d['hash']}))
        break
" <<< "$EXISTING_DASHBOARDS" 2>/dev/null || true)

    if [ -n "$existing" ]; then
        # Update existing dashboard
        local did hash body
        did=$(python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" <<< "$existing")
        hash=$(python3 -c "import sys,json; print(json.load(sys.stdin)['hash'])" <<< "$existing")
        body=$(python3 -c "
import sys, json
d = json.load(sys.stdin)
d['dashboardId'] = '$did'
print(json.dumps(d))
" < "$file")
        if echo "$body" | api PUT "/dashboards/${did}?hash=${hash}" --data-binary @- > /dev/null; then
            echo "  [updated]  $title"
        else
            echo "  [FAILED]   $title" >&2
        fi
    else
        # Create new dashboard
        if api POST "/dashboards" --data-binary @"$file" > /dev/null; then
            echo "  [created]  $title"
        else
            echo "  [FAILED]   $title" >&2
        fi
    fi
}

for f in "$SCRIPT_DIR"/dashboards/*.dashboard.json; do
    [ -f "$f" ] || continue
    provision_dashboard "$f"
done

# --------------------------------------------------------------------------- #
# Alerts (best-effort — requires data streams to exist)
# --------------------------------------------------------------------------- #
echo ""
echo "Alerts"
echo "------"

# Bootstrap the default log stream if it doesn't exist yet.
# Alerts require the stream to be present — seeding one entry creates it.
STREAMS=$(api GET "/streams?type=logs" 2>/dev/null || echo '{"list":[]}')
HAS_LOGS=$(python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('list') else 'no')" <<< "$STREAMS")

if [ "$HAS_LOGS" = "no" ]; then
    api POST "/default/_json" \
        -d '[{"_timestamp":0,"level":"info","body":"_stream_init","service_name":"theo"}]' > /dev/null \
        && echo "  [seeded]   default log stream" \
        || echo "  [FAILED]   could not seed log stream" >&2
fi

# Ensure the "silent" destination exists (webhook to self, effectively a noop)
EXISTING_DESTS=$(api GET "/alerts/destinations" || echo '[]')
HAS_SILENT=$(python3 -c "
import sys, json
dests = json.load(sys.stdin)
print('yes' if any(d.get('name') == 'silent' for d in dests) else 'no')
" <<< "$EXISTING_DESTS")

if [ "$HAS_SILENT" = "no" ]; then
    api POST "/alerts/destinations" \
        -d '{"name":"silent","url":"http://localhost:5080","method":"post","template":"prebuilt_discord","headers":{},"skip_tls_verify":true}' > /dev/null \
        && echo "  [created]  destination: silent" \
        || echo "  [FAILED]   destination: silent" >&2
fi

# Fetch existing alerts
EXISTING_ALERTS=$(curl -sf \
    -H "Authorization: $O2_AUTH" \
    "${O2_URL}/api/v2/${O2_ORG}/alerts" 2>/dev/null || echo '{"list":[]}')

provision_alert() {
    local file="$1"
    local name
    name=$(python3 -c "import sys,json; print(json.load(sys.stdin)['name'])" < "$file")

    local existing_id
    existing_id=$(python3 -c "
import sys, json
data = json.load(sys.stdin)
for a in data.get('list', []):
    if a.get('name') == '$name':
        print(a.get('alert_id', ''))
        break
" <<< "$EXISTING_ALERTS" 2>/dev/null || true)

    if [ -n "$existing_id" ]; then
        if curl -sf -X PUT \
            -H "Authorization: $O2_AUTH" \
            -H "Content-Type: application/json" \
            "${O2_URL}/api/v2/${O2_ORG}/alerts/${existing_id}" \
            --data-binary @"$file" > /dev/null 2>/dev/null; then
            echo "  [updated]  $name"
        else
            echo "  [FAILED]   $name" >&2
        fi
    else
        if curl -sf -X POST \
            -H "Authorization: $O2_AUTH" \
            -H "Content-Type: application/json" \
            "${O2_URL}/api/v2/${O2_ORG}/alerts" \
            --data-binary @"$file" > /dev/null 2>/dev/null; then
            echo "  [created]  $name"
        else
            echo "  [FAILED]   $name" >&2
        fi
    fi
}

for f in "$SCRIPT_DIR"/alerts/*.alert.json; do
    [ -f "$f" ] || continue
    provision_alert "$f"
done

echo ""
echo "Done."
