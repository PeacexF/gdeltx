#!/usr/bin/env bash
# Capture real GDELT responses into tests/fixtures/.
#
# Only fetches what is still missing, so re-running after a partial failure
# costs nothing extra. Retries a 429 once with a long pause, because GDELT
# rate-limits the artlist path hard.
#
# Usage:  bash scripts/capture-fixtures.sh

set -u

UA="gdeltx/0.1.0 (+https://github.com/PeacexF/gdeltx)"
DEST="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures"
API="https://api.gdeltproject.org/api/v2"
PACE=20
RETRY_PAUSE=60

mkdir -p "$DEST"
printf '%-26s %-6s %-8s %s\n' NAME HTTP BYTES PREVIEW
printf '%.0s-' {1..94}; echo

fetch () {
  local name="$1" url="$2" out="$DEST/$1" code
  code=$(curl -sSL -A "$UA" --max-time 60 -o "$out" -w '%{http_code}' "$url" 2>/dev/null) || code=ERR
  if [ "$code" = "429" ]; then
    sleep "$RETRY_PAUSE"
    code=$(curl -sSL -A "$UA" --max-time 60 -o "$out" -w '%{http_code}' "$url" 2>/dev/null) || code=ERR
  fi
  printf '%-26s %-6s %-8s %s\n' "$name" "$code" \
    "$(wc -c < "$out" | tr -d ' ')" \
    "$(head -c 90 "$out" | tr -d '\n' | tr -s ' ')"
  [ "$code" = "200" ] || { mv "$out" "$out.FAILED" 2>/dev/null; }
  sleep "$PACE"
}

want () { [ -s "$DEST/$1" ] || fetch "$@"; }

# DOC artlist — the contended endpoint, and the one Phase 2 needs most.
want doc_artlist.json \
  "$API/doc/doc?query=OpenAI&mode=artlist&format=json&maxrecords=10&timespan=1d"

# Context 2.0 — mode=artlist is required; coverage is only the past 72 hours.
want context_artlist.json \
  "$API/context/context?query=OpenAI&mode=artlist&format=json&timespan=24H&maxrecords=10"

# GEO 2.0 — mode is required; timespan caps at 7 days.
want geo_pointdata.geojson \
  "$API/geo/geo?query=OpenAI&mode=pointdata&format=geojson&timespan=1d"

# Bulk file index (redirects to https, hence -L).
want lastupdate.txt \
  "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"

echo
echo "Already captured, skipped: doc_timeline_2017.json doc_error_maxrecords.txt"
echo "                          doc_rate_limited.txt context_empty.json"
echo "Non-200 responses are renamed *.FAILED; re-run to retry only those."
