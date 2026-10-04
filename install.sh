#!/usr/bin/env bash
# pi-bundle — Installer (Schritt 6). Idempotent, laut, nichts still
# überschreiben (Clean-Slate, CONCEPT §10).
#
# Legt den Bundle-Root, das Konfig-Skelett, den pib-Symlink auf PATH und die
# pi-Registrierungen an (Skills, Session-Hooks, systemd-User-Units) und ruft
# am Ende `pib doctor`, um Struktur + Konfig laut zu prüfen. Erfindet keine
# Instanz-Daten, überschreibt keine bestehenden Dateien (Konflikt → lauter
# Abbruch, Exit != 0) und greift außer dem Repo-Klonen nicht ins Netz.
#
# Nutzung:
#   bash install.sh [--system-template ask|yes|no]
#
# Overrides (Tests/Dev/Sandbox — im Regelbetrieb unverändert lassen):
#   PI_BUNDLE_HOME   Instanz-Root      (Default: ~/.pi/pi-bundle)
#   PIB_BIN_DIR      pib-Symlink-Ort   (Default: ~/.local/bin)
#   PI_SKILLS_DIR    Skill-Verzeichnis (Default: ~/.agents/skills)
#   PI_HOOKS_DIR     Hook-Extensions   (Default: ~/.pi/agent/extensions)
#   PIB_SYSTEMD_DIR  systemd-User-Units(Default: ~/.config/systemd/user)
#   PIB_SYSTEM_MD    SYSTEM.md-Ziel    (Default: ~/.pi/agent/SYSTEM.md)
#   PIB_NO_SYSTEMD=1 systemd-Aktivierung überspringen (nur Units kopieren)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Argumente -------------------------------------------------------------
SYSTEM_TEMPLATE="ask"
case "${1:-}" in
  "" ) ;;
  --system-template )
      SYSTEM_TEMPLATE="${2:-ask}"
      if [ $# -gt 2 ]; then
        echo "[install] FEHLER: zu viele Argumente." >&2
        exit 2
      fi
      ;;
  *  )
      echo "[install] FEHLER: unbekanntes Argument: $1" >&2
      exit 2
      ;;
esac
case "$SYSTEM_TEMPLATE" in
  ask|yes|no) ;;
  *) echo "[install] FEHLER: --system-template muss ask|yes|no sein "
         "(war '$SYSTEM_TEMPLATE')." >&2; exit 2 ;;
esac

info() { echo "[install] $*"; }
fail() { echo "[install] FEHLER: $*" >&2; exit 1; }

_cleanup_tmp() { if [ -n "${TMP:-}" ] && [ -e "$TMP" ]; then rm -rf "$TMP"; fi; return 0; }
trap _cleanup_tmp EXIT

# --- Rolle: Bundle-Root auflösen -------------------------------------------
BUNDLE_ROOT="${PI_BUNDLE_HOME:-$HOME/.pi/pi-bundle}"
BUNDLE_ROOT="${BUNDLE_ROOT/#\~/$HOME}"
BUNDLE_ROOT="$(cd "$BUNDLE_ROOT" 2>/dev/null && pwd || echo "$BUNDLE_ROOT")"
if [ ! -d "$BUNDLE_ROOT" ]; then
  mkdir -p "$BUNDLE_ROOT"
  BUNDLE_ROOT="$(cd "$BUNDLE_ROOT" && pwd)"
fi

# Weiteres Instanz-Auflösung der overridbaren Ziele.
PIB_BIN_DIR="${PIB_BIN_DIR:-$HOME/.local/bin}";    PIB_BIN_DIR="${PIB_BIN_DIR/#\~/$HOME}"
PI_SKILLS_DIR="${PI_SKILLS_DIR:-$HOME/.agents/skills}"; PI_SKILLS_DIR="${PI_SKILLS_DIR/#\~/$HOME}"
PI_HOOKS_DIR="${PI_HOOKS_DIR:-$HOME/.pi/agent/extensions}"; PI_HOOKS_DIR="${PI_HOOKS_DIR/#\~/$HOME}"
PIB_SYSTEMD_DIR="${PIB_SYSTEMD_DIR:-$HOME/.config/systemd/user}"; PIB_SYSTEMD_DIR="${PIB_SYSTEMD_DIR/#\~/$HOME}"
SYSTEM_TARGET="${PIB_SYSTEM_MD:-$HOME/.pi/agent/SYSTEM.md}"; SYSTEM_TARGET="${SYSTEM_TARGET/#\~/$HOME}"

info "Bundle-Root: $BUNDLE_ROOT"
info "pib-Symlink: $PIB_BIN_DIR/pib"

# ===========================================================================
# 1. Root + Struktur (existiert bereits → Meldung, kein Überschreiben)
# ===========================================================================
STRUCTURE_DIRS=(
  memory memory/topics memory/domains
  memory/staging/topics memory/staging/_domains
  memory/staging/done memory/staging/rejected
  memory/digest/queue memory/digest/done memory/digest/failed
  worker worker/queue/pending worker/queue/running
  worker/queue/completed worker/queue/failed worker/output
  lib hooks roles
)
for rel in "${STRUCTURE_DIRS[@]}"; do
  if [ ! -d "$BUNDLE_ROOT/$rel" ]; then
    mkdir -p "$BUNDLE_ROOT/$rel"
    info "Verzeichnis angelegt: $rel/"
  fi
done

if [ ! -f "$BUNDLE_ROOT/memory/INDEX.md" ]; then
  printf '# INDEX — pi-bundle Speicher\n\n(leer — Spiegel des Bestands; wird von der Engine fortgeschrieben)\n' \
    > "$BUNDLE_ROOT/memory/INDEX.md"
  info "memory/INDEX.md angelegt."
else
  info "memory/INDEX.md existiert bereits — unverändert."
fi

# ===========================================================================
# 2. lib/ aus dem Repo installieren (Code-Kopie; nur geänderte Dateien)
# ===========================================================================
refresh_dir() {
  local src="$1" dst="$2" any=0 rel tgt
  mkdir -p "$dst"
  while IFS= read -r -d '' f; do
    rel="${f#"$src"/}"
    tgt="$dst/$rel"
    if [ -d "$f" ]; then
      mkdir -p "$tgt"
    elif [ ! -e "$tgt" ] || ! cmp -s "$f" "$tgt"; then
      mkdir -p "$(dirname "$tgt")"
      cp -p "$f" "$tgt"
      any=1
    fi
  done < <(find "$src" -print0)
  if [ "$any" -eq 1 ]; then
    info "Code aktualisiert: $src/ → $dst/"
  fi
}
refresh_dir "$REPO_DIR/lib" "$BUNDLE_ROOT/lib"

# Rollen-Templates (roles/*.md) dorthin, wo die Konfig-Rollen sie erwarten
# (BUNDLE_ROOT/roles/ — Center-of-Truth des `pib doctor`-Instanz-Checks).
# Vorhandene Templates werden nie überschrieben.
for tf in "$REPO_DIR"/roles/*.md; do
  [ -f "$tf" ] || continue
  base="$(basename "$tf")"
  if [ ! -f "$BUNDLE_ROOT/roles/$base" ]; then
    cp -p "$tf" "$BUNDLE_ROOT/roles/$base"
    info "Rollen-Template installiert: roles/$base"
  else
    info "Rollen-Template vorhanden (gelassen): roles/$base"
  fi
done

# ===========================================================================
# 3. Konfig-Skelett (nur wenn keine config.toml existiert; nie überschreiben)
# ===========================================================================
if [ ! -f "$BUNDLE_ROOT/config.toml" ]; then
  cp "$REPO_DIR/config.example.toml" "$BUNDLE_ROOT/config.toml"
  info "config.toml angelegt (Skelett aus config.example.toml)."
else
  info "config.toml existiert bereits — gelassen, nicht überschrieben."
fi
# ===========================================================================
# 4. pib auf PATH (Symlink)
# ===========================================================================
mkdir -p "$PIB_BIN_DIR"
# Der pib-Eintrag braucht das Exec-Bit (Symlink auf die kopierte lib/pib.py).
chmod +x "$BUNDLE_ROOT/lib/pib.py"
PIB_TARGET="$BUNDLE_ROOT/lib/pib.py"
PIB_LINK="$PIB_BIN_DIR/pib"
if [ -L "$PIB_LINK" ] && [ "$(readlink "$PIB_LINK")" = "$PIB_TARGET" ]; then
  info "pib-Symlink bereits korrekt: $PIB_LINK"
elif [ -e "$PIB_LINK" ] || [ -L "$PIB_LINK" ]; then
  fail "pib existiert bereits unter $PIB_LINK und zeigt nicht auf $PIB_TARGET — Konflikt, nichts überschrieben."
else
  ln -s "$PIB_TARGET" "$PIB_LINK"
  info "pib-Symlink angelegt: $PIB_LINK → $PIB_TARGET"
fi

# ===========================================================================
# 5. Skills registrieren (Namens-Konflikt → lauter Abbruch)
# ===========================================================================
mkdir -p "$PI_SKILLS_DIR"
for skill_dir in "$REPO_DIR"/skills/*/; do
  [ -d "$skill_dir" ] || continue
  name="$(basename "$skill_dir")"
  dest="$PI_SKILLS_DIR/$name"
  if [ -e "$dest" ]; then
    if diff -r -q "$skill_dir" "$dest" >/dev/null 2>&1; then
      info "Skill vorhanden (identisch): $name"
    else
      fail "Skill-Konflikt: $dest existiert bereits mit anderem Inhalt (nichts überschrieben)."
    fi
  else
    cp -R "$skill_dir" "$dest"
    info "Skill registriert: $name → $PI_SKILLS_DIR/"
  fi
done

# ===========================================================================
# 6. Session-Hook-Extension registrieren (TS — pi lädt nur TS/JS-Module)
# ===========================================================================
# pi lädt unter ~/.pi/agent/extensions nur TypeScript/JavaScript-Module
# (docs/extensions.md). Die Session-Hooks laufen daher als TS-Extension
# (hooks/pi-bundle.ts → pi-bundle.ts): INDEX-Injektion bei session_start,
# Digest-Spawn bei session_shutdown. Die Bash-Vorlagen im Repo
# (hooks/session-start, hooks/session-end) bleiben manuelle Schnittstelle
# (Tests/Dev) und werden nicht registriert.
mkdir -p "$PI_HOOKS_DIR"
EXT_SRC="$REPO_DIR/hooks/pi-bundle.ts"
EXT_DEST="$PI_HOOKS_DIR/pi-bundle.ts"
[ -f "$EXT_SRC" ] || fail "Hook-Extension-Vorlage fehlt: $EXT_SRC"
if [ -e "$EXT_DEST" ]; then
  if cmp -s "$EXT_SRC" "$EXT_DEST"; then
    info "Hook-Extension vorhanden (identisch): pi-bundle.ts"
  else
    fail "Hook-Extension-Konflikt: $EXT_DEST existiert bereits mit anderem Inhalt (nichts überschrieben)."
  fi
else
  cp -p "$EXT_SRC" "$EXT_DEST"
  info "Hook-Extension registriert: pi-bundle.ts → $PI_HOOKS_DIR/"
fi
# Veraltete Bash-Hook-Kopien früherer Installationen aufräumen (pi lädt sie
# nie). Nur eigene, erkennbare Artefakte entfernen; Fremde bleiben unangetastet.
for hook in session-end session-start; do
  dest="$PI_HOOKS_DIR/$hook"
  if [ -f "$dest" ]; then
    if cmp -s "$REPO_DIR/hooks/$hook" "$dest"; then
      rm -f "$dest"
      info "Veralteter Bash-Hook entfernt (pi lädt nur TS/JS): $dest"
    else
      info "Fremde Datei belassen: $dest (weicht von der Bash-Hook-Vorlage ab)."
    fi
  fi
done

# ===========================================================================
# 7. systemd-User-Units (Templates mit Instanz-Werten; Konflikt → Abbruch)
# ===========================================================================
# sweep_zeit aus der config.toml (Default 02:00); OnCalendar deterministisch
# aus der zentralen Konfig — nie inline im Dienst.
SWEEP_ZEIT="02:00"
if [ -f "$BUNDLE_ROOT/config.toml" ]; then
  SWEEP_ZEIT="$(sed -n 's/^sweep_zeit[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' \
                "$BUNDLE_ROOT/config.toml" | head -n1)"
  SWEEP_ZEIT="${SWEEP_ZEIT:-02:00}"
fi
ONCAL="*-*-* ${SWEEP_ZEIT}:00"
SWEEP_ZEIT_RE="^[0-9]{1,2}:[0-9]{2}$"
if [[ ! "$SWEEP_ZEIT" =~ $SWEEP_ZEIT_RE ]]; then
  fail "sweep_zeit '$SWEEP_ZEIT' ist kein HH:MM-Wert (config.toml [trigger])."
fi

mkdir -p "$PIB_SYSTEMD_DIR"
for unit in bundle-sweep.service bundle-sweep.timer bundle-worker.service; do
  src="$REPO_DIR/hooks/$unit"
  [ -f "$src" ] || fail "Unit-Vorlage fehlt: $src"
  tmp="$(mktemp)"
  TMP="$tmp"
  sed -e "s|%h/.pi/pi-bundle|$BUNDLE_ROOT|g" \
      -e "s|%h/.local/bin/pib|$PIB_BIN_DIR/pib|g" \
      -e "s|^OnCalendar=.*|OnCalendar=$ONCAL|" \
      "$src" > "$tmp"
  dest="$PIB_SYSTEMD_DIR/$unit"
  if [ -e "$dest" ]; then
    if cmp -s "$tmp" "$dest"; then
      info "systemd-Unit vorhanden (identisch): $unit"
      rm -f "$tmp"; TMP=""
    else
      rm -f "$tmp"; TMP=""
      fail "systemd-Konflikt: $dest existiert bereits mit anderem Inhalt (nichts überschrieben)."
    fi
  else
    mv "$tmp" "$dest"; TMP=""
    info "systemd-Unit installiert: $unit → $PIB_SYSTEMD_DIR/"
  fi
done

if [ "${PIB_NO_SYSTEMD:-0}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now bundle-sweep.timer >/dev/null
  systemctl --user enable bundle-worker.service >/dev/null
  info "systemd: daemon-reload + Timer/Service aktiviert."
  info "  Timer:   systemctl --user list-timers bundle-sweep.timer"
  info "  Service: systemctl --user status bundle-worker.service"
  # Linger-Check (laut, nicht abbrechend): User-Timer feuern nur mit aktiver
  # Login-Session oder aktiviertem Linger — nachts (02:00-Sweep) ist der
  # Normalfall Linger. Ohne ihn findet kein Nacht-Sweep statt.
  if command -v loginctl >/dev/null 2>&1; then
    RUN_USER="${USER:-$(id -un)}"
    if loginctl show-user "$RUN_USER" 2>/dev/null | grep -qi '^Linger=yes'; then
      info "systemd-Linger aktiv — der Nacht-Sweep feuert auch ohne aktive Session."
    else
      info "⚠️  systemd-Linger ist AUS — bundle-sweep.timer feuert ohne aktive Login-Session NICHT (kein Nacht-Sweep)."
      info "    Nachziehen: loginctl enable-linger $RUN_USER"
    fi
  fi
else
  info "systemd-Aktivierung übersprungen (PIB_NO_SYSTEMD oder systemctl fehlt) — Units liegen in $PIB_SYSTEMD_DIR/."
fi

# ===========================================================================
# 8. Optionale SYSTEM.md-Vorlage (nur wenn keine existiert; nie still)
# ===========================================================================
_system_template() {
  cat <<'SYS'
# SYSTEM — Agent-Grundset (pi-bundle)

Du bist ein Agent in einer pi-Installation, die pi-bundle nutzt: **eine
Einheit** aus Memory (gepflegte Wissensstände mit kontrolliertem Schreibkanal)
und Worker (asynchrone Aufgaben mit Rollen). Beide teilen sich einen Root
(~/.pi/pi-bundle), eine zentrale Konfiguration (config.toml), ein CLI (`pib`)
und eine Infrastruktur.

## Memory-Operatoren
Nutze den Skill `memory-operatoren`: Lesen ist frei (`pib topic read`,
`pib pipeline status`). Schreibend sind nur die vier Einzelakte
(`pib topic create`, `pib domain create`, `pib package commit`,
`pib package reject`) — je Eintrag Freigabe durch den User, keine stillen
Schreibakte.

## Task-Delegation
Nutze den Skill `task-definition` VOR jeder Delegation: strukturierte
Argumente (kein Freitext-Prompt), absolute Pfade, Einzeiler-Regel
(`--task` ≤ max_task_chars), Protokoll/Deliverable-Trennung. Ein Task trägt
nur Inhalte; Rolle, Tools, Modell und Wissensquellen kommen aus der Rolle.

## Zentral-Konfig
Die einzige Konfigurationsquelle ist `<root>/config.toml` (user-managed, nie
system-geschrieben). Prüfe deinen Installationszustand jederzeit mit
`pib doctor`. Keine Secrets in der Konfig; Vollständigkeit laut, nichts still
raten oder ergänzen.
SYS
}

if [ -f "$SYSTEM_TARGET" ]; then
  info "SYSTEM.md existiert bereits: $SYSTEM_TARGET — gelassen (nie still überschrieben)."
elif [ "$SYSTEM_TEMPLATE" = "yes" ]; then
  mkdir -p "$(dirname "$SYSTEM_TARGET")"
  _system_template > "$SYSTEM_TARGET"
  info "SYSTEM.md-Vorlage angelegt: $SYSTEM_TARGET"
elif [ "$SYSTEM_TEMPLATE" = "ask" ]; then
  if [ -t 0 ]; then
    read -r -p "[install] SYSTEM.md-Vorlage anlegen unter $SYSTEM_TARGET? (ja/nein) [nein]: " ans
    case "${ans:-nein}" in
      ja|j|y|yes)
        mkdir -p "$(dirname "$SYSTEM_TARGET")"
        _system_template > "$SYSTEM_TARGET"
        info "SYSTEM.md-Vorlage angelegt: $SYSTEM_TARGET" ;;
      *)
        info "SYSTEM.md-Vorlage übersprungen (Antwort: '${ans:-nein}')." ;;
    esac
  else
    info "SYSTEM.md-Vorlage übersprungen (kein interaktives TTY — '--system-template yes|no' verwenden)."
  fi
else
  info "SYSTEM.md-Vorlage übersprungen (--system-template no)."
fi

# ===========================================================================
# 9. Abschluss: pib doctor (Ergebnis laut melden; Instanz-Daten sind Sache)
# ===========================================================================
echo
info "Abschluss: pib doctor"
set +e
if "$PIB_BIN_DIR/pib" doctor; then
  DOCTOR_GREEN=1
else
  DOCTOR_GREEN=0
fi
set -e

echo
if [ "$DOCTOR_GREEN" -eq 1 ]; then
  info "✓ pib doctor grün — Installation vollständig."
else
  info "✗ pib doctor meldet Lücken (oben). Installation ok — Instanz-Daten in config.toml und Rollen-Templates nachziehen, dann erneut pib doctor."
fi
info "Installation abgeschlossen."
exit 0
