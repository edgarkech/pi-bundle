# TESTPLAN — Clean-Slate-Test (Schritt 7)

**Ziel:** Beweis NF1 — pi-bundle läuft ab nackter pi-Installation ohne Vorprojekte oder bestehende Memory-Struktur.
**Erste Test-Maschine:** lurch (frisch, Clean-Slate) · **Basis:** ANFORDERUNGEN v1.2 (NF1, NF3, F21), CONCEPT v0.1 (§10)

## Vorbereitung der Maschine (vor dem Test)

Nacktes pi, sonst nichts (NF1):

- pi installiert (Sessions, Skills, Tool-Set)
- git installiert (für den Klon; das Repo ist öffentlich, kein Token nötig)
- python3 ≥ 3.11 (tomllib — die pi-Installation bringt das mit)

## Testablauf

### 1. Installation

```
git clone https://github.com/edgarkech/pi-bundle.git
cd pi-bundle && ./install.sh --system-template ask
```

- Install-Ziele: `~/.pi/pi-bundle/` (Root), `~/.local/bin/pib` (PATH), Skills + Hooks registriert, systemd-User-Units (Sweep-Timer + Watchdog-Service) installiert und enable
- `ask` fragt nach der optionalen SYSTEM.md-Vorlage (nur wenn keine existiert; nie still)

### 2. Konfiguration (Instanz-Daten)

- `config.toml` ausfüllen: `models.available` (lokale Aliase oder CLOUD), `roles` (Modell je Rolle), `dispatch`, `trigger`
- `pib doctor` → **grün** (Exit 0) — Struktur, Konfig, Instanz-Daten vollständig

### 3. Smoke-Test über den echten Prozess

- **Memory:** `pib topic create` → Paket-Loop (`package commit` je Eintrag → `read` → `--provenienz` → Snapshot existiert) → `pipeline status`
- **Worker (echter pi-Spawn):** `pib task create --role coder …` → `pib watchdog --once` → Protokoll (`result.md`, `reflection.md`, `trace.md`) + Deliverable geprüft
- **Verdrahtung:** fake Session-Protokoll → `pib digest run` → Digest im Fach + curator-Task eingereiht → Watchdog verarbeitet → Paket im Staging
- **Hooks:** `hooks/session-start` (INDEX-Injektion) · `hooks/session-end` (ruft `digest run`)
- **systemd:** Timer + Service aktiv (`systemctl --user status`)

### 4. Erfolgskriterien (Beweis NF1)

- `install.sh` läuft ab nacktem pi ohne Fehler (laut, idempotent)
- `pib doctor` grün
- Echter pi-Spawn: Task mit Protokoll + Deliverable
- Verdichtungs-Lauf: Digest → curator-Task → Paket im Staging
- Timer/Service aktiv
- Kein stiller Zustand: alle Fehler laut

## Nach dem Test

- lurch bleibt als erste Test-/Betriebs-Instanz des Bundles
- Funde/Diskrepanzen aus dem Test → Fix-Tasks nach dem etablierten Muster
