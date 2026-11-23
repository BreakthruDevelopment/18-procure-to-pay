#!/usr/bin/env bash
# Replay smoke: run `p2p demo` and assert the printed story.
# Live smoke after deploy: `scripts/smoke.sh --url https://app18.breakthruventures.ai`
#   asserts GET /health returns the version and a passing replay round-trip.
# Default path uses no network. Both this script and `p2p demo` build a private
# database in a fresh mkdtemp directory and ignore any configured path.
set -euo pipefail

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
pass() { echo "ok - $*"; }

if [ "${1:-}" = "--url" ]; then
  [ -n "${2:-}" ] || fail "usage: scripts/smoke.sh --url <base-url>"
  base="${2%/}"
  health=$(curl -fsS "$base/health") || fail "health endpoint unreachable"
  echo "$health" | grep -q '"status":"ok"' || fail "health not ok: $health"
  echo "$health" | grep -q '"version"' || fail "health missing version: $health"
  echo "$health" | grep -q '"replay":"ok"' || fail "health replay did not pass: $health"
  pass "GET /health version and replay round-trip"
  echo "SMOKE PASS"
  exit 0
fi

[ $# -eq 0 ] || fail "usage: scripts/smoke.sh [--url <base-url>]"

scratch=$(mktemp -d "${TMPDIR:-/tmp}/p2p-smoke.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
# Do not pass this directory, or P2P_DATABASE, to the demo: p2p demo builds its
# own file and must not honour a configured path.
unset P2P_DATABASE || true

out="$scratch/demo.out"
if command -v p2p >/dev/null 2>&1; then
  p2p demo | tee "$out"
else
  uv run --frozen p2p demo | tee "$out"
fi

grep -q "repaired true" "$out" || fail "ingest repair missing"
grep -q "verdict match" "$out" || fail "clean match missing"
grep -q "pst_000001" "$out" || fail "staged posting key missing"
grep -q "decision applied" "$out" || fail "approve missing"
grep -q "verdict variance" "$out" || fail "quantity variance missing"
grep -q "expected 38 actual 50" "$out" || fail "quantity expected/actual missing"
grep -q '"items": 12' "$out" || fail "batch item count missing"
grep -q '"failed": 1' "$out" || fail "batch failure missing"
grep -q "escalate the credit exceeds 2,500" "$out" || fail "escalation missing"
grep -q "demo ok (replay, no network)" "$out" || fail "demo did not finish on replay"
pass "replay demo printed ingest, repair, match, review, approve, batch, dispute, escalation"
echo "SMOKE PASS"
