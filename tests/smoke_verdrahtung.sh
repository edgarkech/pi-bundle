#!/usr/bin/env bash
# pi-bundle Smoke-Test (Schritt 4c) — Verdrahtung über den echten Prozess.
#
# Setzt PI_BUNDLE_HOME auf ein temporäres Sandbox-Verzeichnis mit eigener
# Konfig (einstelliges [dispatch]) und prüft über den echten pib-Prozess:
#   1. pib digest run gegen ein fake Session-Protokoll → Digest im Fach
#      (memory/digest/queue/) + curator-Task in worker/queue/pending (kein
#      echter pi-Spawn — die Tasks landen nur in der Queue) + task.json-Meta.
#   2. Doppelverdichtung ausgeschlossen: zweiter Lauf → noop, kein doppelter
#      Task (Idempotenz, F4).
#   3. pib digest sweep: Auto-Commit-Zweig (auto=ja-Zeile mit fertiger
#      Staging-Datei → commit mit Provenienz „auto") + Räumung nach digest/done/.
#   4. Erhaltungsgarantie: unverdichtete Session in sessions_root wird vom
#      Sweep nachverdichtet (Digest + Task angelegt).
#   5. hooks/session-end und hooks/session-start als Skripte aufrufbar und
#      funktional (Session-End ruft digest run; Session-Start injiziert INDEX).
# Sandbox wird aufgeräumt; kein Schreibzugriff außerhalb der Sandbox + Repo.
#
# Nutzung: bash tests/smoke_verdrahtung.sh

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIB="$REPO_DIR/lib/pib.py"
PYTHON="python3"
SID="20261003-1000-smoke"

failures=0
pass() { echo "  [ok]   $1"; }
fail() { echo "  [FAIL] $1" >&2; failures=$((failures+1)); }

# ---- Sandbox (aufräumen am Ende) --------------------------------------------
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pib-verdrahtung.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT
export PI_BUNDLE_HOME="$SANDBOX"

echo "== Sandbox: $PI_BUNDLE_HOME"

for d in memory sessions worker; do
  mkdir -p "$SANDBOX/$d"
done

# Eigene Konfig (einstelliges [dispatch]: topic, auto=ja, ziel=pi-demo).
cat > "$SANDBOX/config.toml" <<TOML
[paths]
memory = "memory"
sessions_root = "$SANDBOX/sessions"

[worker]
poll_seconds = 1
default_model = "qwen38-27b"
default_timeout_seconds = 600
max_task_chars = 250
deliverable_pflicht_roles = []

[roles.curator]
tools = ["read", "write", "bash"]
wissensquellen = ["learnings/curator-lessons.md"]
model = "qwen38-27b"

[models]
available = ["qwen38-27b"]
default = "qwen38-27b"

[[dispatch]]
session_typ = "default"
profil = "topic"
auto = "ja"
ziel = "pi-demo"

[trigger]
sweep_zeit = "02:00"
session_ende = "an"
max_task_attempts = 2
TOML

# Ziel-Topic anlegen (der auto=ja-Lauf committet darauf).
"$PYTHON" "$PIB" topic create --name pi-demo --titel "Demo-Verdrahtung" \
  --spielart projekt >/dev/null 2>&1 \
  && pass "topic pi-demo angelegt" || fail "topic pi-demo nicht angelegt"

# Fake Session-Protokoll (kein Topic-Signal → Ziel-Topic kommt aus dispatch.ziel).
SESSION_FILE="$SANDBOX/sessions/$SID.jsonl"
cat > "$SESSION_FILE" <<'JSONL'
{"type":"session_start","timestamp":"2026-10-03T10:00:00Z","message":{"role":"user","content":"Hallo, neues Vorhaben"}}
{"type":"message","timestamp":"2026-10-03T10:01:00Z","message":{"role":"assistant","content":"Verstanden."}}
JSONL

# Helm: führt pib aus und prüft Exit-Code + optionales stdout-Muster.
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
    fail "$desc (Exit $rc, erwartet $expect)"; cat "$SANDBOX/.err" >&2; return
  fi
  if [ -n "$pat" ] && ! grep -q "$pat" <<<"$out"; then
    fail "$desc (Muster '$pat' fehlt in Ausgabe)"; return
  fi
  pass "$desc"
}

# =============================================================================
echo
echo "== 1. digest run: Digest + curator-Task + task.json-Meta ----------------"
check "digest run (processed)" 0 "processed" digest run --session "$SESSION_FILE"

if [ -f "$SANDBOX/memory/digest/queue/$SID.digest.md" ]; then
  pass "Digest liegt in memory/digest/queue/"
else
  fail "Digest fehlt in memory/digest/queue/"
fi
if [ -f "$SANDBOX/memory/digest/queue/$SID.task.json" ]; then
  pass "task.json-Meta liegt bei (Dispatch/Fortschritt)"
else
  fail "task.json-Meta fehlt"
fi

PENDING_COUNT="$(ls "$SANDBOX/worker/queue/pending/" 2>/dev/null | wc -l)"
if [ "$PENDING_COUNT" -eq 1 ]; then
  pass "genau 1 curator-Task eingereiht (pending)"
else
  fail "erwartet 1 pending Task, gefunden: $PENDING_COUNT"
fi
# Kein echter pi-Spawn: kein running-/completed-Übergang, nichts erzeugt.
if ls "$SANDBOX/worker/queue/running/"*.json >/dev/null 2>&1; then
  fail "unerwartet: ein Task läuft bereits (ohne echten pi-Spawn)"
else
  pass "kein Task in running/ (nur Queue, kein Spawn)"
fi

TASK_ID="$(ls "$SANDBOX/worker/queue/pending/" 2>/dev/null | head -n1 | sed 's/\.json$//')"
if [ -n "$TASK_ID" ]; then
  pass "curator-Task-ID: $TASK_ID"
else
  fail "curator-Task-ID nicht extrahierbar"
fi

# =============================================================================
echo
echo "== 2. Doppelverdichtung ausgeschlossen (Idempotenz, F4) ----------------"
PENDING_BEFORE="$(ls "$SANDBOX/worker/queue/pending/" 2>/dev/null | wc -l)"
OUT2="$("$PYTHON" "$PIB" digest run --session "$SESSION_FILE" 2>/dev/null)"
rc2=$?
if [ "$rc2" -eq 0 ] && grep -q '"noop"' <<<"$OUT2"; then
  pass "zweiter digest run → status noop (änderungslos, vollwertig)"
else
  fail "zweiter digest run sollte noop sein (Exit $rc2, out: $OUT2)"
fi
PENDING_AFTER="$(ls "$SANDBOX/worker/queue/pending/" 2>/dev/null | wc -l)"
if [ "$PENDING_AFTER" -eq "$PENDING_BEFORE" ]; then
  pass "kein doppelter Task (pending $PENDING_BEFORE → $PENDING_AFTER)"
else
  fail "doppelter Task angelegt (pending $PENDING_BEFORE → $PENDING_AFTER)"
fi

# =============================================================================
echo
echo "== 3. digest sweep: Auto-Commit (auto=ja) + Räumung nach done/ ---------"
# Curator-Abschluss simulieren (kein echter pi-Lauf): pending → completed.
mkdir -p "$SANDBOX/worker/queue/completed"
for f in "$SANDBOX/worker/queue/pending/"*.json; do
  [ -e "$f" ] && mv "$f" "$SANDBOX/worker/queue/completed/" 2>/dev/null || true
done

# Fertige Staging-Datei (Topic-Paket) für den auto=ja-Lauf ablegen.
STAGE="$SANDBOX/memory/staging/pi-demo"
mkdir -p "$STAGE"
PAKET="$STAGE/$SID.pi-demo.proposal.json"
cat > "$PAKET" <<JSON
{
  "session_id": "$SID",
  "erzeugt_durch": "curator/topic",
  "datum": "2026-10-03 10:05",
  "paket_typ": "topic",
  "ziel": "pi-demo",
  "turn_coverage": {"turns_seen": 2, "turns_total": 2},
  "digest_markierungen": [],
  "eintraege": [
    {
      "id": "e1",
      "typ": "topic",
      "ziel": "pi-demo",
      "operation": "UPSERT_EINTRAG",
      "abschnitt": "entscheidungen",
      "schluessel": "autotest",
      "text": "Auto-Commit im Smoke-Test nachgewiesen.",
      "beleg": {"turns": [1]}
    }
  ]
}
JSON

SWEEP_OUT="$("$PYTHON" "$PIB" digest sweep 2>"$SANDBOX/.err")"
rc3=$?
if [ "$rc3" -eq 0 ]; then
  pass "digest sweep (Exit 0)"
else
  fail "digest sweep exit $rc3"; cat "$SANDBOX/.err" >&2
fi
echo "    sweep: $SWEEP_OUT"

if grep -q "auto=" <<<"$SWEEP_OUT" && grep -q "done=" <<<"$SWEEP_OUT"; then
  pass "sweep-Bericht nennt auto=/done="
else
  fail "sweep-Bericht unerwartet: $SWEEP_OUT"
fi

# Räumung: Digest + Meta nach digest/done/.
if [ -f "$SANDBOX/memory/digest/done/$SID.digest.md" ]; then
  pass "Digest nach digest/done/ geräumt"
else
  fail "Digest fehlt in digest/done/"
fi
# Provenienz „auto" als Session-Label nachgewiesen.
PROV="$SANDBOX/memory/topics/pi-demo/provenienz.md"
if [ -f "$PROV" ] && grep -q "auto" "$PROV"; then
  pass "Provenienz dokumentiert Herkunft 'auto'"
else
  fail "Provenienz ohne 'auto'-Herkunft ($PROV)"
fi
# Topic-Lesung zeigt den committeten Eintrag.
if "$PYTHON" "$PIB" topic read --topic pi-demo 2>/dev/null | grep -q "Auto-Commit im Smoke-Test"; then
  pass "committeter Eintrag im Topic pi-demo lesbar"
else
  fail "committeter Eintrag nicht lesbar"
fi

# =============================================================================
echo
echo "== 4. Erhaltungsgarantie: unverdichtete Session wird nachverdichtet ----"
SID2="20261003-1100-smoke2"
SESSION2="$SANDBOX/sessions/$SID2.jsonl"
cat > "$SESSION2" <<'JSONL'
{"type":"session_start","timestamp":"2026-10-03T11:00:00Z","message":{"role":"user","content":"Zweite Session"}}
{"type":"message","timestamp":"2026-10-03T11:01:00Z","message":{"role":"assistant","content":"Ok."}}
JSONL

SWEEP2="$("$PYTHON" "$PIB" digest sweep 2>/dev/null)"
rc4=$?
if [ "$rc4" -eq 0 ]; then
  pass "digest sweep (Erhaltungsgarantie) Exit 0"
else
  fail "digest sweep exit $rc4"
fi
if grep -q "catch_up=" <<<"$SWEEP2"; then
  pass "sweep-Bericht nennt catch_up (nachverdichtet)"
else
  fail "sweep-Bericht ohne catch_up: $SWEEP2"
fi
if [ -f "$SANDBOX/memory/digest/queue/$SID2.digest.md" ]; then
  pass "unverdichtete Session2 nachverdichtet (Digest in queue/)"
else
  fail "Session2-Digest fehlt in queue/"
fi
# Die nachverdichtete Session2 erzeugte einen weiteren curator-Task.
P2="$(ls "$SANDBOX/worker/queue/pending/" 2>/dev/null | wc -l)"
if [ "$P2" -ge 1 ]; then
  pass "nachverdichtete Session2 reihte curator-Task ein (pending=$P2)"
else
  fail "kein Task für Session2 eingereiht"
fi

# =============================================================================
echo
echo "== 5. Hooks als Skripte aufrufbar und funktional -----------------------"
# --- session-start: injiziert den INDEX --------------------------------------
INDEX_FILE="$SANDBOX/memory/INDEX.md"
printf '## Demo-Index\ntopics: pi-demo\n' > "$INDEX_FILE"
SS_OUT="$(PI_BUNDLE_INDEX="$INDEX_FILE" "$REPO_DIR/hooks/session-start" 2>"$SANDBOX/.err-hs")"
rcss=$?
if [ "$rcss" -eq 0 ] && grep -q "TOPIC-INDEX" <<<"$SS_OUT" \
   && grep -q "Demo-Index" <<<"$SS_OUT"; then
  pass "session-start injiziert den INDEX (Block ausgegeben)"
else
  fail "session-start Fehler (Exit $rcss): $SS_OUT"; cat "$SANDBOX/.err-hs" >&2
fi
# Fehlender INDEX → laut, Exit != 0 (kein stiller Zustand).
if PI_BUNDLE_INDEX="$SANDBOX/fehlt.md" "$REPO_DIR/hooks/session-start" \
     >/dev/null 2>&1; then
  fail "session-start ohne INDEX sollte scheitern"
else
  pass "session-start ohne INDEX → Exit != 0 (laut)"
fi

# --- session-end: ruft digest run für die endende Session --------------------
SID3="20261003-1200-smoke3"
SESSION3="$SANDBOX/sessions/$SID3.jsonl"
cat > "$SESSION3" <<'JSONL'
{"type":"session_start","timestamp":"2026-10-03T12:00:00Z","message":{"role":"user","content":"End-Hook Session"}}
JSONL
set +e
SE_OUT="$(PIB_BIN="$PIB" PIB_PYTHON="$PYTHON" \
          "$REPO_DIR/hooks/session-end" "$SESSION3" 2>"$SANDBOX/.err-se")"
rcse=$?
set -e
if [ "$rcse" -eq 0 ]; then
  pass "session-end (Exit 0)"
else
  fail "session-end exit $rcse"; cat "$SANDBOX/.err-se" >&2
fi
if [ -f "$SANDBOX/memory/digest/queue/$SID3.digest.md" ]; then
  pass "session-end rief digest run → Digest für Session3 im Fach"
else
  fail "session-end erzeugte keinen Digest (digest run nicht gerufen?)"
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
