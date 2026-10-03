# pi-bundle

**Eine Einheit: Memory + Worker.** pi-bundle bündelt eine Memory-Engine
(gepflegte Wissensstände mit kontrolliertem Schreibkanal und nachgelagerter
Verdichtung) und einen Worker (asynchrone Aufgaben mit Rollen) zu **einem**
Produkt mit **einem** Root, **einer** zentralen Konfiguration, **einem** CLI
(`pib`) und **einer** Infrastruktur. Zielgruppe: ein pi-Agent, der Wissen
hält und nebenläufige Aufgaben delegiert — aufgeräumt, laut, ohne stille
Zustände.

## Was das Produkt kann

| Komponente | Aufgabe |
|---|---|
| **Memory** | Topics (`sockel.md`, `provenienz.md`, `snapshots/`), Domänen-Wissen, Staging-Pakete mit Freigabe je Eintrag, atomare Commits, `INDEX.md` als Spiegel |
| **Verdichtung** | Der Verdichter (Rolle `curator`) baut aus Session-Digests Freigabe-Pakete; nachgelagert als Nacht-Sweep (`pib digest sweep`) |
| **Worker + Watchdog** | file-basierte Queue, **serial** (ein laufender Task), Rollen-Setup je Task, Modell-Auflösung & -Entladen (konfigurierbar) |
| **Trigger** | Session-End-Hook + systemd-User-Timer (Nacht-Sweep) — Infrastruktur in Code, keine Agent-Arbeit in der Session |
| **CLI `pib`** | eine manuelle Oberfläche für beide Komponenten |
| **Installer `install.sh`** | legt Root, Konfig-Skelett, PATH, pi-Registrierungen an; idempotent, laut |
| **`pib doctor`** | Struktur-/Konfig-Prüfung (F21) — meldet Lücken laut, ratet nichts |
| **Zwei Skills** | Agent-seitige Verhaltens-Doku: Memory-Operatoren, Task-Definition |

## Quick-Start

Voraussetzung ist nur eine pi-Installation (der Agent läuft auf pi; die
Instanz-Daten entstehen je Installation). Es gibt keine Abhängigkeit von
Vorprojekten.

```bash
# 1. Klonen
git clone <dein-pi-bundle-Repo> pi-bundle && cd pi-bundle

# 2. Installieren
bash install.sh            # oder: bash install.sh --system-template yes|no

# 3. Konfig ausfüllen (Instanz-Daten — der Installer ratet nichts)
$EDITOR ~/.pi/pi-bundle/config.toml
#   - Modell-Aliase in [models].available + default_model in [worker]
#   - Rollen-Templates unter ~/.pi/pi-bundle/roles/<rolle>.md anlegen
#     (Vorlage: roles/curator.md)

# 4. Prüfen — grün heißt: Struktur + Konfig + Instanz-Daten vollständig
pib doctor                 # Exit 0 grün · Exit != 0 mit Lücken-Liste

# 5. Smoke-Tests
bash tests/smoke_memory.sh
bash tests/smoke_worker.sh
bash tests/smoke_verdrahtung.sh
bash tests/smoke_installer.sh
```

Nach dem Install liegt der Bundle-Root unter `~/.pi/pi-bundle/` (überschreibbar
via `PI_BUNDLE_HOME`), `pib` als Symlink unter `~/.local/bin/pib`, und die
Skill-/Hook-/systemd-Registrierungen sind aktiv (Session-End-Hook, Watchdog,
Nacht-Sweep-Timer).

> **Hinweis:** Der Installer erfindet keine Instanz-Daten und überschreibt
> nichts still. Läuft er schon einmal, meldet er vorhandene Ressourcen
> (Config, Skills, Hooks, Units) und lässt sie unverändert; ein echter Konflikt
> bricht laut ab (Exit ≠ 0).

## Datenmodell (nur kurz — Details im CONCEPT)

Ein Root = eine Einheit = eine Backup-Grenze = ein Deinstallieren:

```
~/.pi/pi-bundle/
├── config.toml           # die EINE Konfigurationsquelle (user-managed)
├── memory/               # Wissensstände (§5)
│   ├── INDEX.md          # Spiegel des Bestands (nie SSoT)
│   ├── topics/<name>/    # sockel.md · provenienz.md · snapshots/
│   ├── domains/<name>.md
│   ├── staging/          # topics/ · _domains/ · done/ · rejected/
│   └── digest/           # queue/ · done/ · failed/
├── worker/               # queue/pending|running|completed|failed · output/<task>/
├── lib/                  # Code: Engine, Verdichter, Worker, CLI, Watchdog
├── hooks/                # Trigger-Implementierungen + systemd-Assets
└── roles/                # Rollen-Templates (<rolle>.md)
```

**Kern-Prinzipien:**
- **Ein Root, eine Konfig, ein CLI** — keine verstreuten Datenpfade.
- **Nichts still:** jede Aktion ist laut; keine stillen Zustände, keine stillen
  Write-Overwrites; Fehler werden klassifiziert und gemeldet, kein Auto-Retry.
- **Instanz-Daten je Installation** — Code und Schema sind maschinenunabhängig.
- **Keine Secrets** in der Konfig; vertrauliche Werte bleiben an pi-üblichen
  Orten außerhalb des Roots.

## Dokumente (Doku-Satz)

| Artefakt | Zweck |
|---|---|
| `ANFORDERUNGEN.md` | Anforderungen (Basis) |
| `CONCEPT.md` | Design (§4 CLI/doctor, §10 Installer, §11 Doku-Satz) |
| `config.example.toml` | Konfig-Skelett — selbst-dokumentierend, Instanz-Daten |
| `skills/memory-operatoren/SKILL.md` | Memory-Bedienung aus Agent-Sicht |
| `skills/task-definition/SKILL.md` | Delegations-Disziplin aus Agent-Sicht |
| `roles/curator.md` | Rollen-Template des Verdichters (Anlage weiterer Rollen) |
| `tests/` | Smoke-Tests (Memory, Worker, Verdrahtung, Installer) |

## CLI-Grammatik (Auszug)

```
# Memory
pib topic read     --topic <n> [--provenienz] [--snapshot <ts>-<sid>]
pib topic create   --name <n> --titel <t> --spielart <spielart> [--projekt <abs>] [--beschreibung <d>]
pib domain create  --name <n> --titel <t> --entry-key <k>
pib package commit --paket <abs> [--eintraege <id>]...
pib package reject --paket <abs> [--eintraege <id>]... [--grund <t>]
pib pipeline status

# Worker
pib task create|status|validate|cleanup ...
pib watchdog [--once]

# Verdichtung
pib digest run --session <abs>
pib digest sweep [--dry-run]

# Setup/Prüfung
pib doctor
```

## Lizenz

Open Source (öffentliches Repo). Details siehe Projekt-Repository.
