#!/usr/bin/env bash
# Replay smoke: run `p2p demo` and assert the printed story.
# Live smoke after deploy: `scripts/smoke.sh --url https://app18.breakthruventures.ai`
#   asserts GET /health returns a version and a passing replay round-trip.
#   Add `--version sha-<commit>` to assert WHICH build answered: without it a deploy
#   that silently kept serving the previous image passes the only live check app 18
#   has, because the key being present says nothing about its value.
# Default path uses no network. Both this script and `p2p demo` build a private
# database in a fresh mkdtemp directory and ignore any configured path.
set -euo pipefail

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
pass() { echo "ok - $*"; }

usage() { fail "usage: scripts/smoke.sh [--url <base-url> [--version <expected>]]"; }

if [ "${1:-}" = "--url" ]; then
  [ -n "${2:-}" ] || usage
  base="${2%/}"
  expected=""
  if [ $# -gt 2 ]; then
    { [ $# -eq 4 ] && [ "$3" = "--version" ] && [ -n "$4" ]; } || usage
    expected="$4"
  fi
  # --max-time: a hung endpoint must fail the smoke, not hold the deploy open.
  health=$(curl -fsS --max-time 20 "$base/health") || fail "health endpoint unreachable"
  # Tolerant of whitespace: a pretty-printed body is still a healthy one.
  echo "$health" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' || fail "health not ok: $health"
  echo "$health" | grep -Eq '"replay"[[:space:]]*:[[:space:]]*"ok"' ||
    fail "health replay did not pass: $health"
  # The VALUE, not just the key: presence cannot tell a fresh deploy from a stale one.
  version=$(printf '%s' "$health" |
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [ -n "$version" ] || fail "health reported no version: $health"
  if [ -n "$expected" ] && [ "$version" != "$expected" ]; then
    fail "health reports version $version, expected $expected: a stale image is still serving"
  fi
  pass "GET /health reports version $version and a passing replay round-trip"
  echo "SMOKE PASS"
  exit 0
fi

[ $# -eq 0 ] || usage

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
