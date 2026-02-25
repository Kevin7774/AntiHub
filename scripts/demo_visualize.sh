#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: scripts/demo_visualize.sh <repo_url> [ref]"
  exit 1
fi

REPO_URL="$1"
REF="${2:-}"
API_BASE="${API_BASE:-http://localhost:8010}"
OUTPUT_DIR="${OUTPUT_DIR:-./visual_output}"
export REPO_URL REF API_BASE OUTPUT_DIR

mkdir -p "$OUTPUT_DIR"

if [ -z "${OPENCLAW_BASE_URL:-}" ]; then
  export OPENCLAW_BASE_URL="http://127.0.0.1:8787"
  echo "OPENCLAW_BASE_URL not set. Starting local OpenClaw mock at $OPENCLAW_BASE_URL"
  echo "Make sure AntiHub is started with OPENCLAW_BASE_URL=${OPENCLAW_BASE_URL}"
  python3 -m openclaw.server >/tmp/openclaw.log 2>&1 &
  OPENCLAW_PID=$!
  trap 'kill ${OPENCLAW_PID} >/dev/null 2>&1 || true' EXIT
  sleep 1
fi

payload=$(python3 - <<PY
import json
import os
repo = os.environ.get('REPO_URL')
ref = os.environ.get('REF')
body = {"repo_url": repo}
if ref:
    body["ref"] = ref
print(json.dumps(body))
PY
)

case_resp=$(curl -s -X POST "$API_BASE/cases" \
  -H "Content-Type: application/json" \
  -d "$payload")

case_id=$(python3 - <<PY
import json,sys
resp = json.loads(sys.stdin.read())
print(resp.get('case_id') or '')
PY
<<<"$case_resp")

if [ -z "$case_id" ]; then
  echo "Failed to create case: $case_resp"
  exit 1
fi

echo "Created case: $case_id"

curl -s -X POST "$API_BASE/cases/$case_id/visualize" \
  -H "Content-Type: application/json" \
  -d '{"force": false}' >/dev/null

echo "Waiting for visuals..."
for i in $(seq 1 180); do
  status_resp=$(curl -s "$API_BASE/cases/$case_id")
  ready=$(python3 - <<PY
import json,sys
resp=json.loads(sys.stdin.read())
print('1' if resp.get('visual_ready') else '0')
PY
<<<"$status_resp")
  if [ "$ready" = "1" ]; then
    break
  fi
  sleep 2
  if [ $i -eq 180 ]; then
    echo "Timed out waiting for visuals"
    exit 1
  fi
done

visuals=$(curl -s "$API_BASE/cases/$case_id/visuals")

python3 - <<PY
import json,sys,os,urllib.request
resp=json.loads(sys.stdin.read())
output_dir=os.environ.get('OUTPUT_DIR','./visual_output')
os.makedirs(output_dir, exist_ok=True)
assets = resp.get('assets') or []
files=[]
for asset in assets:
    for f in asset.get('files') or []:
        files.append(f)

for f in files:
    name=f.get('name')
    url=f.get('url')
    if not name or not url:
        continue
    target=os.path.join(output_dir, name)
    urllib.request.urlretrieve(os.environ.get('API_BASE','http://localhost:8010') + url, target)

# rename mp4 if present
for name in os.listdir(output_dir):
    if name.endswith('.mp4'):
        src=os.path.join(output_dir, name)
        dst=os.path.join(output_dir, 'output.mp4')
        if src != dst:
            try:
                os.replace(src, dst)
            except Exception:
                pass
        break
print('Downloaded to', output_dir)
PY
<<<"$visuals"

echo "Artifacts: $OUTPUT_DIR/repo_index.json, $OUTPUT_DIR/spotlights.json, $OUTPUT_DIR/storyboard.json, $OUTPUT_DIR/output.mp4"
echo "Open UI at: $API_BASE"
