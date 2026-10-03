#!/usr/bin/env bash
# pi-bundle Smoke-Test (Schritt 4b) — Worker-Kern über den echten Prozess.
#
# Setzt PI_BUNDLE_HOME auf ein temporäres Sandbox-Verzeichnis, legt die
# Konfig aus config.example.toml an und prüft über den echten pib-Prozess:
#   Konfig-Fehler laut · task create (atomar → pending) · Validierungs-Gate
#   (Modell nicht in Whitelist → Exit != 0, kein Task) · task status über die
#   Queues · Watchdog-Boot mit leerer Queue (`pib watchdog --once` → no-op,
#   Exit 0, lauter Zustandswechsel) · task validate · task cleanup --dry-run.
#
# Die echte Ausführung (pi-Spawn) wird bewusst erst im Clean-Slate-Test auf
# lurch bewiesen (hier nur Boot mit leerer Queue).
# Exit 0 nur, wenn ALLE Prüfungen bestehen; die Sandbox wird aufgeräumt.
# Kein Schreibzugriff außerhalb der Sandbox und des Repos.
#
# Nutzung: bash tests/smoke_worker.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIB="$REPO_DIR/lib/pib.py"
CONFIG_TEMPLATE="$REPO_DIR/config.example.toml"
PYTHON="python3"

failures=0
pass() { echo "  [ok]   $1"; }
fail() { echo "  [FAIL] $1" >&2; failures=$((failures+1)); }

# ---- Sandbox (aufräumen am Ende) --------------------------------------------
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pib-worker-smoke.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT
export PI_BUNDLE_HOME="$SANDBOX"

echo "== Sandbox: $PI_BUNDLE_HOME"

# Konfig aus der Vorlage + Worker-Skelett vorbereiten.
cp "$CONFIG_TEMPLATE" "$SANDBOX/config.toml"
mkdir -p "$SANDBOX/memory"

# Helm: führt pib aus und prüft (Exit-Code, optionales stdout-Muster).
# check <beschreibung> <erwarteter_exit> [<grep_muster|->] -- <args>...
check() {
  local desc="$1"; shift
  local expect="$1"; shift
  local pat="${1:-}"; [ "$pat" = "-" ] && pat=""; shift
  local out rc
  set +e
  out="$("$PYTHON" "$PIB" "$@" 2>"$SANDBOX/.err")"
  rc=$?
  set -e
  if [ "$rc" -ne "$expect" ]; then
    fail "$desc (Exit $rc, erwartet $expect)"
    cat "$SANDBOX/.err" >&2
    return
  fi
  if [ -n "$pat" ] && ! grep -q "$pat" <<<"$out"; then
    fail "$desc (Muster '$pat' fehlt in Ausgabe)"
    return
  fi
  pass "$desc"
}

# =============================================================================
echo
echo "== 1. Fehlende Konfig schlägt laut fehl (kein Default-Raten) -----------"
EMPTY="$(mktemp -d "${TMPDIR:-/tmp}/pib-worker-empty.XXXXXX")"
if PI_BUNDLE_HOME="$EMPTY" "$PYTHON" "$PIB" task create \
     --role coder --task "Ohne Konfig" --deliverable /tmp/a.py \
     >"$EMPTY/.out" 2>"$EMPTY/.err"; then
  fail "ohne config.toml sollte der Aufruf scheitern (Exit != 0)"
elif [ "$(grep -c 'status.*error' "$EMPTY/.err")" -eq 0 ]; then
  fail "ohne config.toml fehlt die laute Fehlermeldung"
else
  pass "fehlende Konfig -> Exit != 0 + laute Fehlermeldung"
fi
rm -rf "$EMPTY"

# =============================================================================
echo
echo "== 2. Watchdog-Boot mit leerer Queue (--once => no-op, Exit 0) ---------"
check "watchdog --once (leere Queue)" 0 "Keine pending Tasks" \
  watchdog --once

# =============================================================================
echo
echo "== 3. task create -> pending (atomar, validiert) ------------------------"
DEL="$SANDBOX/ergebnis/a.py"
CREATE_OUT="$(PI_BUNDLE_HOME="$SANDBOX" "$PYTHON" "$PIB" task create \
     --role coder \
     --task "Unit-Tests für Poller schreiben" \
     --deliverable "$DEL" \
     --model qwen38-27b 2>"$SANDBOX/.err")"
if [ $? -ne 0 ]; then
  fail "task create sollte exit 0 liefern ($CREATE_OUT)"; cat "$SANDBOX/.err" >&2
else
  pass "task create (coder, Deliverable, Modell) -> Exit 0"
fi
grep -q "pending" <<<"$CREATE_OUT" && pass "create meldet 'pending'" \
  || fail "create meldet kein 'pending'"

# Task-ID extrahieren (coder-<ts>).
TASK_ID="$(grep -oE 'coder-[0-9]{8}-[0-9]+' <<<"$CREATE_OUT" | head -n1)"
if [ -z "$TASK_ID" ]; then
  fail "Task-ID nicht extrahierbar aus: $CREATE_OUT"
else
  pass "Task-ID extrahiert: $TASK_ID"
fi

# Atomare Einreihung: Datei liegt in worker/queue/pending/.
if [ -f "$SANDBOX/worker/queue/pending/$TASK_ID.json" ]; then
  pass "Task liegt atomar in worker/queue/pending/"
else
  fail "pending-Datei fehlt: worker/queue/pending/$TASK_ID.json"
fi
# Keine tmp-Reste (atomares Staging).
if [ -e "$SANDBOX/worker/queue/pending/$TASK_ID.tmppending" ]; then
  fail "tmp-Staging-Rest vorhanden (nicht atomar aufgeräumt)"
else
  pass "kein tmp-Staging-Rest"
fi

# =============================================================================
echo
echo "== 4. task status über die Queues (pending) ----------------------------"
check "task status <id> (pending)" 0 "PENDING" \
  task status "$TASK_ID"
check "task status <id> --json" 0 "pending" \
  task status "$TASK_ID" --json
check "task status unbekannte id (Exit 4)" 4 "-" \
  task status "coder-falsch-id"

# =============================================================================
echo
echo "== 5. Validierungs-Gate: Modell nicht in Whitelist -> Exit != 0 --------"
BOGUS_COUNT_BEFORE="$(ls "$SANDBOX/worker/queue/pending/" 2>/dev/null | wc -l)"
if "$PYTHON" "$PIB" task create \
     --role coder --task "Bogus-Modell" \
     --deliverable "$DEL" --model bogus-alias \
     >"$SANDBOX/.out" 2>"$SANDBOX/.err"; then
  fail "bogus-Modell sollte scheitern (Exit != 0)"
else
  pass "bogus-Modell -> Exit != 0"
fi
grep -q "VALIDATOR-FAIL" "$SANDBOX/.err" \
  && pass "lauter Validierungs-Fehler (VALIDATOR-FAIL)" \
  || fail "fehlende VALIDATOR-FAIL-Meldung: $(cat "$SANDBOX/.err")"
BOGUS_COUNT_AFTER="$(ls "$SANDBOX/worker/queue/pending/" 2>/dev/null | wc -l)"
if [ "$BOGUS_COUNT_AFTER" -eq "$BOGUS_COUNT_BEFORE" ]; then
  pass "kein Task eingereiht (Count unverändert)"
else
  fail "trotz Fehler wurde ein Task eingereiht (Count $BOGUS_COUNT_BEFORE -> $BOGUS_COUNT_AFTER)"
fi

# =============================================================================
echo
echo "== 6. Weitere Validierungs-Gates ----------------------------------------"
if "$PYTHON" "$PIB" task create \
     --role unbekannt --task "X" --deliverable "$DEL" \
     >"$SANDBOX/.out" 2>"$SANDBOX/.err"; then
  fail "unbekannte Rolle sollte scheitern"; else
  pass "unbekannte Rolle -> Exit != 0"
fi

# =============================================================================
echo
echo "== 7. task validate (Rolle aus pending-Datei, Standalone-Diagnose) -----"
check "task validate gültige Task-Datei" 0 "OK" \
  task validate "$SANDBOX/worker/queue/pending/$TASK_ID.json"

# Ungültige Task-Datei (Model nicht in Whitelist) -> Exit 1.
BAD_ID="coder-bad-20261003-000000"
mkdir -p "$SANDBOX/worker/queue/pending"
BAD_FILE="$SANDBOX/worker/queue/pending/$BAD_ID.json"
cat > "$BAD_FILE" <<JSON
{
  "id": "$BAD_ID",
  "status": "pending",
  "role": "coder",
  "model": "bogus-alias",
  "task": "Ungültig",
  "spec": null,
  "deliverables": ["$DEL"],
  "prompt": "Ungültig\n\nDELIVERABLE: $DEL",
  "output_path": "output/$BAD_ID/",
  "timeout": 300
}
JSON
if "$PYTHON" "$PIB" task validate "$BAD_FILE" >"$SANDBOX/.out" 2>"$SANDBOX/.err"; then
  fail "task validate mit bogus-Modell sollte scheitern"; else
  pass "task validate (bogus-Modell) -> Exit != 0"
fi
grep -q "model=" "$SANDBOX/.err" \
  && pass "Validate-Meldung nennt Whitelist-Verstoß" \
  || fail "Validate-Meldung nicht laut"

# =============================================================================
echo
echo "== 8. task cleanup --dry-run (noch nichts alt) --------------------------"
check "task cleanup --dry-run (Exit 0)" 0 "Keine alten Tasks" \
  task cleanup --days 1 --dry-run

# =============================================================================
echo
echo "============================================================"
if [ "$failures" -eq 0 ]; then
  echo "SMOKE-TEST OK (alle Prüfungen bestanden)"
  echo "============================================================"
  exit 0
fi
echo "SMOKE-TEST FEHLGESCHLAGEN: $failures Prüfung(en) fehlgeschlagen"
echo "============================================================"
exit 1
