#!/usr/bin/env bash
# pi-bundle Smoke-Test (Schritt 4a) — Memory-Engine über den echten Prozess.
#
# Setzt PI_BUNDLE_HOME auf ein temporäres Sandbox-Verzeichnis, legt die
# Konfig aus config.example.toml an und prüft über den echten pib-Prozess:
#   Konfig-Fehler laut · topic create · Paket-Loop (Teil-Commit → read →
#   --provenienz → Snapshot → pipeline status → read --snapshot) ·
#   package reject-Zweig.
# Exit 0 nur, wenn ALLE Prüfungen bestehen; die Sandbox wird aufgeräumt.
# Kein Schreibzugriff außerhalb der Sandbox und des Repos.
#
# Nutzung: bash tests/smoke_memory.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIB="$REPO_DIR/lib/pib.py"
CONFIG_TEMPLATE="$REPO_DIR/config.example.toml"
PYTHON="python3"

failures=0
pass() { echo "  [ok]   $1"; }
fail() { echo "  [FAIL] $1" >&2; failures=$((failures+1)); }

# ---- Sandbox (aufräumen am Ende) --------------------------------------------
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pib-smoke.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT
export PI_BUNDLE_HOME="$SANDBOX"

echo "== Sandbox: $PI_BUNDLE_HOME"

# Konfig aus der Vorlage + Memory-Basis-Skelett vorbereiten.
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
EMPTY="$(mktemp -d "${TMPDIR:-/tmp}/pib-empty.XXXXXX")"
if PI_BUNDLE_HOME="$EMPTY" "$PYTHON" "$PIB" topic read --topic nix \
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
echo "== 2. Topic anlegen ----------------------------------------------------"
check "topic create pi-demo" 0 "CREATE_TOPIC: Topic 'pi-demo' angelegt" \
  topic create --name pi-demo --titel "Demo-Port" --spielart projekt \
  --beschreibung "Smoke-Test-Topic"

check "topic create bei Kollision scheitert (Exit 1)" 1 "-" \
  topic create --name pi-demo --titel "Nochmal" --spielart einzel

check "Sockel-Teil prüfen (read im Normal-Modus)" 0 "## Steckbrief" \
  topic read --topic pi-demo

# =============================================================================
echo
echo "== 3. Paket-Loop: Teil-Commit → read → provenienz → snapshot -----------"

# Proposal-Datei (2 Einträge; nur e01 wird im ersten Commit freigegeben).
STAGE="$SANDBOX/memory/staging/pi-demo"
mkdir -p "$STAGE"
PAKET="$STAGE/po-20261003.proposal.json"
cat > "$PAKET" <<'JSON'
{
  "session_id": "20261003-100000-smoke",
  "erzeugt_durch": "smoke-test",
  "datum": "2026-10-03 10:00",
  "paket_typ": "topic",
  "ziel": "pi-demo",
  "turn_coverage": {"turns_seen": 1, "turns_total": 1},
  "digest_markierungen": [],
  "eintraege": [
    {
      "id": "e01",
      "typ": "topic",
      "operation": "UPSERT_EINTRAG",
      "abschnitt": "entscheidungen",
      "schluessel": "Sprache",
      "text": "Wir setzen auf Python 3.11+ (tomllib, keine Dependencies).",
      "beleg": {"turns": [1]}
    },
    {
      "id": "e02",
      "typ": "topic",
      "operation": "SET_FELD",
      "abschnitt": "steckbrief",
      "schluessel": "beschreibung",
      "text": "Beschreibung nach dem ersten Teil-Commit.",
      "beleg": {"turns": [1]}
    }
  ]
}
JSON

# Teil-Commit: nur e01 freigeben.
check "package commit (Teilmenge e01)" 0 "UPSERT_EINTRAG" \
  package commit --paket "$PAKET" --eintraege e01

# Nach dem Teil-Commit verbleibt e02 im Paket (noch im Staging-Fach).
check "Teil-commit: e02 noch im Staging-Block" 0 '"e02"' \
  topic read --topic pi-demo

# Normal-Read: der Wissen-Eintrag (IDs gestrippt) sichtbar.
check "read: Wissen-Eintrag 'Sprache' sichtbar" 0 "Sprache" \
  topic read --topic pi-demo

# Provenienz on-demand: die interne Eintrags-ID (e0001) dokumentiert.
check "read --provenienz: Eintrag dokumentiert" 0 "e0001" \
  topic read --topic pi-demo --provenienz

# Snapshot nach dem Commit existiert (Stelle-4-Mechanik).
if [ -n "$(ls -A "$SANDBOX/memory/topics/pi-demo/snapshots/" 2>/dev/null)" ]; then
  pass "Snapshot-Verzeichnis nach Commit belegt"
else
  fail "Snapshot-Verzeichnis nach Commit ist leer"
fi

# Vollständiger Commit des Restpakets (e02).
check "package commit (Rest e02)" 0 "SET_FELD" \
  package commit --paket "$PAKET"

# Paket leer → nach done/ verschoben.
if [ -f "$SANDBOX/memory/staging/pi-demo/done/po-20261003.proposal.json" ]; then
  pass "leeres Paket nach done/ verschoben"
else
  fail "leeres Paket fehlt in done/"
fi

# =============================================================================
echo
echo "== 4. pipeline status --------------------------------------------------"
check "pipeline status läuft" 0 "-" pipeline status
"$PYTHON" "$PIB" pipeline status 2>/dev/null \
  | "$PYTHON" -c 'import json,sys
d=json.load(sys.stdin)
assert set(d)=={"queue","failed","offene_sessions"}, d
assert all(isinstance(v,int) and v>=0 for v in d.values()), d'
pass "pipeline status liefert queue/failed/offene_sessions (int)"

# =============================================================================
echo
echo "== 5. package reject (Verwerfungs-Zweig) -------------------------------"
REJ="$STAGE/po-rej.proposal.json"
cat > "$REJ" <<'JSON'
{
  "session_id": "20261003-110000-smoke",
  "erzeugt_durch": "smoke-test",
  "datum": "2026-10-03 11:00",
  "paket_typ": "topic",
  "ziel": "pi-demo",
  "turn_coverage": {"turns_seen": 1, "turns_total": 1},
  "digest_markierungen": [],
  "eintraege": [
    {
      "id": "z01",
      "typ": "topic",
      "operation": "UPSERT_EINTRAG",
      "abschnitt": "entscheidungen",
      "schluessel": "Verworfen",
      "text": "Dieser Eintrag wird verworfen.",
      "beleg": {"turns": [1]}
    }
  ]
}
JSON

check "package reject (--grund)" 0 "rejected" \
  package reject --paket "$REJ" --grund "Smoke: bewusst storniert"

# Verwerfungs-Dokument im rejected/-Fach.
REJ_DOC="$(find "$SANDBOX/memory/staging/pi-demo/rejected" \
            -name '*.rejected.json' 2>/dev/null | head -n1)"
if [ -n "$REJ_DOC" ] && grep -q "Verworfen" "$REJ_DOC"; then
  pass "Verwerfungs-Dokument mit Eintrag abgelegt"
else
  fail "rejected/-Dokument fehlt oder ohne Eintrag"
fi

# =============================================================================
echo
echo "== 6. Snapshot per read --snapshot lesbar (Stelle-4-Rückblick) ---------"
SNAP_NAME="$(ls -A "$SANDBOX/memory/topics/pi-demo/snapshots/" | head -n1)"
if [ -n "$SNAP_NAME" ]; then
  check "read --snapshot $SNAP_NAME" 0 '"snapshot"' \
    topic read --topic pi-demo --snapshot "$SNAP_NAME"
else
  fail "Kein Snapshot für read --snapshot vorhanden"
fi

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
