#!/usr/bin/env bash
# pi-bundle Smoke-Test (Schritt 6) — Installer über eine echte Sandbox.
#
# Setzt PI_BUNDLE_HOME und ALLE pi-Registrierungs-Ziele des Installers auf
# temporäre Sandbox-Verzeichnisse (kein Schreibzugriff auf ~/.local/bin,
# ~/.agents/skills, ~/.pi/agent und ~/.config/systemd/user — die sind im Test
# tabu). Lässt install.sh nicht-interaktiv laufen (--system-template no) und
# prüft:
#   Struktur angelegt · config.toml aus dem Skelett · config.toml wird beim
#   erneuten Lauf NICHT überschrieben · pib-Symlink funktioniert ·
#   systemd-Unit-Templating (Instanz-Pfade) · `pib doctor` meldet die
#   erwarteten Lücken (Instanz-Daten nicht gefüllt → laut, NICHT grün) ·
#   erneuter Lauf idempotent (kein stiller Schaden, kein Konflikt).
# Exit 0 nur, wenn ALLE Prüfungen bestehen; die Sandbox wird aufgeräumt.
# Kein Schreibzugriff außerhalb der Sandbox und des Repos.
#
# Nutzung: bash tests/smoke_installer.sh

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL="$REPO_DIR/install.sh"

failures=0
pass() { echo "  [ok]   $1"; }
fail() { echo "  [FAIL] $1" >&2; failures=$((failures+1)); }

# ---- Sandbox (aufräumen am Ende) --------------------------------------------
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/pib-installer.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

export PI_BUNDLE_HOME="$SANDBOX/root"
export PIB_BIN_DIR="$SANDBOX/bin"
export PI_SKILLS_DIR="$SANDBOX/skills"
export PI_HOOKS_DIR="$SANDBOX/extensions"
export PIB_SYSTEMD_DIR="$SANDBOX/systemd"
export PIB_SYSTEM_MD="$SANDBOX/SYSTEM.md"
export PIB_NO_SYSTEMD=1

echo "== Sandbox: $PI_BUNDLE_HOME"

# =============================================================================
echo
echo "== 1. Erstlauf: install.sh (nicht-interaktiv, --system-template no) ----"
if bash "$INSTALL" --system-template no >"$SANDBOX/install1.log" 2>&1; then
  pass "install.sh Erstlauf -> Exit 0"
else
  fail "install.sh Erstlauf sollte Exit 0 liefern"; cat "$SANDBOX/install1.log" >&2
fi

# ---- Struktur --------------------------------------------------------------
STRUCTURE=(lib hooks roles \
           memory/topics memory/domains \
           memory/staging/topics memory/staging/_domains \
           memory/staging/done memory/staging/rejected \
           memory/digest/queue memory/digest/done memory/digest/failed \
           worker/queue/pending worker/queue/running \
           worker/queue/completed worker/queue/failed worker/output)
for rel in "${STRUCTURE[@]}"; do
  [ -d "$PI_BUNDLE_HOME/$rel" ] && pass "Struktur: $rel/" \
      || fail "Struktur fehlt: $rel/"
done
[ -f "$PI_BUNDLE_HOME/memory/INDEX.md" ] && pass "INDEX.md angelegt" \
    || fail "INDEX.md fehlt"

# ---- Konfig-Skelett --------------------------------------------------------
if [ -f "$PI_BUNDLE_HOME/config.toml" ] && grep -q '^\[paths\]' \
    "$PI_BUNDLE_HOME/config.toml"; then
  pass "config.toml aus dem Skelett (Sektion [paths] vorhanden)"
else
  fail "config.toml fehlt oder ist kein Skelett"
fi

# ---- pib-Symlink -----------------------------------------------------------
if [ -L "$PIB_BIN_DIR/pib" ] && \
   [ "$(readlink "$PIB_BIN_DIR/pib")" = "$PI_BUNDLE_HOME/lib/pib.py" ]; then
  pass "pib-Symlink zeigt auf installiertes lib/pib.py"
else
  fail "pib-Symlink fehlt bzw. falsches Ziel: $PIB_BIN_DIR/pib"
fi

# ---- Skills / Hooks / Rollen-Templates --------------------------------------
[ -f "$PI_SKILLS_DIR/memory-operatoren/SKILL.md" ] && pass "Skill memory-operatoren registriert" \
    || fail "Skill memory-operatoren fehlt"
[ -f "$PI_SKILLS_DIR/task-definition/SKILL.md" ] && pass "Skill task-definition registriert" \
    || fail "Skill task-definition fehlt"
[ -f "$PI_HOOKS_DIR/session-end" ]  && pass "Hook session-end registriert" \
    || fail "Hook session-end fehlt"
[ -x "$PI_HOOKS_DIR/session-end" ]  && pass "Hook session-end ausführbar" \
    || fail "Hook session-end nicht ausführbar"
[ -f "$PI_HOOKS_DIR/session-start" ] && pass "Hook session-start registriert" \
    || fail "Hook session-start fehlt"
# Rollen-Templates + Worker-Basis installiert (Rollen-Parität: der Spawn
# braucht WORKER_SYSTEM.md + je Rolle ein Template am Root).
for rt in WORKER_SYSTEM.md coder.md researcher.md architect.md admin.md \
          curator.md inventory.md; do
  [ -f "$PI_BUNDLE_HOME/roles/$rt" ] && pass "Rollen-Template $rt installiert" \
      || fail "Rollen-Template $rt fehlt"
done

# ---- systemd-Unit-Templating (Instanz-Pfade) -------------------------------
for unit in bundle-sweep.service bundle-sweep.timer bundle-worker.service; do
  [ -f "$PIB_SYSTEMD_DIR/$unit" ] && pass "systemd-Unit installiert: $unit" \
      || fail "systemd-Unit fehlt: $unit"
done
if grep -q "Environment=PI_BUNDLE_HOME=$PI_BUNDLE_HOME" \
    "$PIB_SYSTEMD_DIR/bundle-worker.service"; then
  pass "worker-Service: PI_BUNDLE_HOME auf Instanz-Root gesetzt"
else
  fail "worker-Service: PI_BUNDLE_HOME nicht auf Instanz-Root gesetzt"
fi
if grep -q "ExecStart=$PIB_BIN_DIR/pib watchdog" \
    "$PIB_SYSTEMD_DIR/bundle-worker.service"; then
  pass "worker-Service: ExecStart = pib watchdog (Symlink-Pfad)"
else
  fail "worker-Service: ExecStart nicht templated"
fi
if grep -q "ExecStart=$PIB_BIN_DIR/pib digest sweep" \
    "$PIB_SYSTEMD_DIR/bundle-sweep.service"; then
  pass "Sweep-Service: ExecStart templated"
else
  fail "Sweep-Service: ExecStart nicht templated"
fi
if grep -q "OnCalendar=\*-\\*-\\* 02:00:00" \
    "$PIB_SYSTEMD_DIR/bundle-sweep.timer"; then
  pass "sweep-Timer: OnCalendar aus Default sweep_zeit (02:00)"
else
  fail "sweep-Timer: OnCalendar fehlt/falsch"
fi

# =============================================================================
echo
echo "== 2. pib doctor (via Symlink) meldet erwartete Lücken laut -----------"
DOCTOR_OUT="$("$PIB_BIN_DIR/pib" doctor 2>&1)"
DOCTOR_RC=$?
if [ "$DOCTOR_RC" -eq 0 ]; then
  pass "pib doctor -> Exit 0 (grün: Rollen-Templates + WORKER_SYSTEM installiert)"
else
  fail "pib doctor sollte auf frischer Installation grün sein"; echo "$DOCTOR_OUT" >&2
fi
if grep -q "Rollen-Template fehlt" <<<"$DOCTOR_OUT"; then
  fail "doctor meldet unerwartet Rollen-Template-Lücken trotz installierter Templates" \
      ; echo "$DOCTOR_OUT" >&2
else
  pass "doctor meldet keine Rollen-Template-Lücken (alle Templates installiert)"
fi

# =============================================================================
echo
echo "== 3. Erneuter Lauf idempotent (kein stiller Schaden, kein Konflikt) ---"
CONFIG_MD5_1="$(md5sum "$PI_BUNDLE_HOME/config.toml" | awk '{print $1}')"
SKILLS_BEFORE="$(find "$PI_SKILLS_DIR" -name SKILL.md | wc -l)"
UNITS_BEFORE="$(ls "$PIB_SYSTEMD_DIR" | wc -l)"

if bash "$INSTALL" --system-template no >"$SANDBOX/install2.log" 2>&1; then
  pass "install.sh Zweitlauf -> Exit 0 (idempotent, kein Konflikt)"
else
  fail "install.sh Zweitlauf sollte Exit 0 liefern (idempotent)"; cat "$SANDBOX/install2.log" >&2
fi

CONFIG_MD5_2="$(md5sum "$PI_BUNDLE_HOME/config.toml" | awk '{print $1}')"
if [ "$CONFIG_MD5_1" = "$CONFIG_MD5_2" ]; then
  pass "config.toml beim Zweitlauf unverändert (nicht überschrieben)"
else
  fail "config.toml wurde beim Zweitlauf verändert (stiller Schaden)"
fi
SKILLS_AFTER="$(find "$PI_SKILLS_DIR" -name SKILL.md 2>/dev/null | wc -l)"
if [ "$SKILLS_BEFORE" -eq "$SKILLS_AFTER" ]; then
  pass "keine doppelten Skills beim Zweitlauf"
else
  fail "Skills-Duplikate: $SKILLS_BEFORE -> $SKILLS_AFTER"
fi
UNITS_AFTER="$(ls "$PIB_SYSTEMD_DIR" | wc -l)"
if [ "$UNITS_BEFORE" -eq "$UNITS_AFTER" ]; then
  pass "keine doppelten systemd-Units beim Zweitlauf"
else
  fail "Unit-Duplikate: $UNITS_BEFORE -> $UNITS_AFTER"
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
