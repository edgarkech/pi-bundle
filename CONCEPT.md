# CONCEPT — pi-bundle

**Status:** Entwurf v0.1 (2026-10-03) — abgeleitet aus ANFORDERUNGEN v1.0 (festgezurrt)
**Home (SSoT):** `~/github-projects/pi-bundle/` · **Zielrepo:** öffentlich auf GitHub (`edgarkech`)
**Einzige Ergänzung über die Paritäts-Basislinie hinaus:** die Struktur-/Konfig-Prüfung `pib doctor` (§4) — wird als F21 in ANFORDERUNGEN nachgetragen. Bewusste Abweichungen von der Basislinie: keine.

Dieses Dokument beschreibt das Design: wie die festgezurrten Anforderungen umgesetzt werden.

## 1. Architektur

pi-bundle ist **eine Einheit**: Memory (gepflegte Wissensstände, kontrollierter Schreibkanal, nachgelagerte Verdichtung) und Worker (asynchrone Aufgaben, Rollen, Protokolle) teilen sich einen Root, eine Konfiguration, ein CLI und eine Infrastruktur. Die Kopplung ist der Kern: der Verdichter ist eine Worker-Rolle, die Verdrahtung läuft über die zentrale Konfiguration.

| Komponente | Aufgabe |
|---|---|
| Memory-Engine | gepflegte Wissensstände, kontrollierter Schreibkanal, Herkunft & Snapshots, Domänen, Index-Spiegel |
| Verdichter (Rolle `curator`) | baut aus Session-Digests Freigabe-Pakete — write-frei, nie committend |
| Worker + Watchdog | file-basierte Queue, serial, Rollen-Setup je Task; Watchdog = Runtime-Daemon |
| Trigger | Session-End-Hook + Nacht-Sweep — Infrastruktur in Code, keine Agent-Arbeit in der Session |
| CLI (`pib`) | eine manuelle Oberfläche über beide Komponenten |
| Installer (`install.sh`) | legt Root, Konfig-Skelett, PATH, pi-Registrierungen an |
| Prüfung (`pib doctor`) | Struktur- und Konfig-Prüfung, laut |
| Skills | Agent-seitige Verhaltens-Doku (Memory-Operatoren, Task-Definition) |

## 2. Runtime-Root & Verzeichnisstruktur

**Ein Root: `~/.pi/pi-bundle/`**, überschreibbar via `PI_BUNDLE_HOME`:

```
~/.pi/pi-bundle/
├── config.toml              # die zentrale Konfiguration (§3)
├── memory/                  # Wissensstände (§5)
│   ├── INDEX.md
│   ├── topics/<name>/       # sockel.md · provenienz.md · snapshots/
│   ├── domains/<name>.md
│   ├── staging/             # topics/ · _domains/ · done/ · rejected/
│   └── digest/              # queue/ · done/ · failed/
├── worker/                  # Task-Daten (§6)
│   ├── queue/
│   └── output/<task-id>/    # ausschließlich result.md · reflection.md · trace.md
├── lib/                     # Code: Engine, Verdichter, Worker, CLI, Watchdog
└── hooks/                   # Trigger-Implementierungen (Session-Ende, Nacht-Sweep)
```

**Begründung:** ein Root = eine Einheit = eine Backup-Grenze, ein Installer, ein Deinstallieren. Der Root liegt im pi-Haus; der pi-seitige Fußabdruck ist minimal (Hook-Registrierung, User-Service, User-Timer). Secrets bleiben an pi-üblichen Orten außerhalb des Roots.

## 3. Zentrale Konfiguration

**Eine Datei: `config.toml`** — die einzige Konfigurationsquelle des Produkts. TOML, mit Kommentaren als Selbst-Dokumentation (der Installer legt ein kommentiertes Skelett ab).

```toml
[paths]         # Instanz-Pfade (Root ergibt sich, Details hier)
[memory]        # Spielarten-Einstellungen, Auto-Commit-Defaults
[worker]        # Poll-Intervalle, Unlade-Verhalten (je Instanz), Standard-Modell
[roles.<name>]  # je Rolle: Tools, Wissensquellen, Modell-Alias
[models]        # Modell-Aliase der Instanz
[dispatch]      # Session-Typ → Verdichtungs-Lauf (Profil, Auto-Verhalten)
[trigger]       # Nacht-Sweep-Zeitpunkt, Session-End-Verhalten
```

**Regeln:**
- **Nie system-geschrieben** — user-managed wie das Agent-Grundset; danach nur manuelle Änderungen.
- **Keine Secrets** — nur Verweise auf pi-übliche Orte.
- Die Dispatch-Sektion ersetzt eine separate Matrix-Datei: eine Quelle, weil die Kopplung der Kern ist.

## 4. CLI / Bedienungsmodell

**Ein Entry-Point: `pib`**, Grammatik `<objekt> <aktion>`:

```
# Memory
pib topic read     --topic <n> [--provenienz] [--snapshot <ts>-<sid>]
pib topic create   --name <n> --titel <t> --spielart <projekt|dauer_betrieb|einzel|austausch>
                   [--projekt <abs>] [--beschreibung <d>]
pib domain create  --name <n> --titel <t> --entry-key <k>   # Domänen-Anlage (ausdrücklicher Einzelakt)
pib package commit --paket <abs> [--eintraege <id>]...
pib package reject --paket <abs> [--eintraege <id>]... [--grund <t>]
pib pipeline status

# Worker
pib task create|status|validate ...   # Flags je nach Task-Format (§6)

# Verdichtung (manuell: Debug/Reparatur; Regelbetrieb über Trigger, §9)
pib digest run [--session <abs>]
pib digest sweep                      # vom systemd-Timer gerufen

# Setup/Prüfung
pib doctor
```

- Die CLI ist die **manuelle** Oberfläche (User + Agent); der Regelbetrieb der Verdichtung läuft über Trigger.
- **`pib doctor`** prüft Struktur und Konfig konsistent und meldet laut: fehlende Verzeichnisse, unvollständige Konfig-Sektionen, fehlende Instanz-Daten. Es ratet nichts.
- Der Installer selbst ist ein separates `install.sh` (Bootstrap: muss existieren, bevor `pib` auf PATH ist — §10).

## 5. Memory-Komponente

**Sockel je Vorhaben** — `memory/topics/<name>/`:
- `sockel.md` — EINE Datei, Bereiche als Abschnitte: Steckbrief / Plan / Journal / Wissen (Entscheidungen, Rahmenbedingungen, Relationen, Dokumente) / Verweise. Spielarten (`projekt|dauer_betrieb|einzel|austausch`) bestimmen die Tiefe; fehlende Bereiche sind Platzhalter, keine Fehler.
- `provenienz.md` — Herkunft je Eintrag (id → Abschnitt/Schlüssel/Datum/Session), on-demand ladbar.
- `snapshots/` — vollständiger Stand je Commit; Rückblick ohne eigene Verlaufs-Datei.

**Kontrollierter Schreibkanal:** Änderung nur als Staging-Paket → Freigabe je Eintrag (oder Paket auf einmal) → atomarer Commit. Validierung vor Schreiben; ein Validierungsfehler wirft den ganzen Commit (Zustand bleibt byte-identisch). Operations-Vokabular: `SET_FELD`, `UPSERT_EINTRAG`, `REMOVE_EINTRAG`, `UPSERT_DOMAIN`. Folgeregeln: Provenienz je angewandtem Eintrag, Snapshot je Commit, Index-Spiegel-Sync im selben Schreibakt; Paket danach leer → `done/`; Verwerfen dokumentiert nach `rejected/`.

**Domänen:** `memory/domains/<name>.md` für nicht-gebundenes Wissen jenseits einzelner Vorhaben — gleich gepflegt (Engine), manuell korrigierbar; Anlage via `pib domain create` (ausdrücklicher Einzelakt, nie still); Pakete über das `_domains`-Fach; die Zieldatei muss existieren (UPSERT_DOMAIN gegen fehlende Datei → Fehler).

**Index:** `memory/INDEX.md` ist Spiegel des Bestands (Vorhaben + Domänen), fortgeschrieben im selben Schreibakt wie die Quelle, nie selbst Quelle.

**Auto-Commit:** nur für Dispatch-Zeilen mit `auto=ja`, nur vom Nacht-Sweep angestoßen, Provenienz „auto". Standard bleibt Freigabe durch den User.

## 6. Worker-Komponente

**Task-Format:** strukturierte Definition, kein Freitext-Prompt — Rolle, Einzeiler, Spec, Deliverable-Pfade als Felder; absolute Pfade; atomares Staging (tmp → `worker/queue/`), Validierung vor Queue-Eintritt. **Einzeiler-Regel:** Task ≤ 250 Zeichen als Einzeiler, sonst erst Konzept + Spec-Datei (außerhalb des Protokollbereichs).

**Queue & Ausführung:** file-basiert, **serial — ein laufender Task** (bewusstes Design). Zustände queued → running → done/failed, laut markiert. **Ein Task = ein pi-Prozess** mit Rollen-Setup: Modell-Alias, erlaubte Tools, Wissensquellen kommen aus der Rolle, nie aus dem Task (der Task trägt nur Inhalte). Modellwahl deterministisch vor dem Run aus der Konfig; das Unlade-Verhalten ist konfigurierbar je Instanz. Externe Provider laufen nur per Konfiguration (User-Entscheidung) — keine automatische Modell-/Cloud-Auswahl.

**Protokoll/Deliverable-Trennung:** `worker/output/<task-id>/` enthält ausschließlich `result.md`, `reflection.md`, `trace.md`. Deliverables landen ausschließlich in deklarierten Pfaden, nie im Protokollbereich.

**Fehler:** klassifiziert und laut gemeldet — kein Auto-Retry, der User entscheidet.

**Timer-Tasks** laufen über denselben Pfad wie manuelle Tasks (kein Sonderweg).

## 7. Watchdog (Worker-Runtime)

Der Watchdog ist die Worker-Runtime: Daemon als systemd-User-Service, While-Loop mit Poll-Intervall, **serial — ein Task gleichzeitig**, Modell-Load/Unload-Polling (konfigurierbar je Instanz), Zustands-Markierung laut. **`--once`-Modus** für manuelle Läufe — ein Task, dann Exit (der Clean-Slate-Smoke-Test nutzt genau das). Konfig aus `[worker]`.

## 8. Skills (Agent-Seite)

Das Produkt schifft zwei Skills mit (der Installer registriert sie in pis Skill-Verzeichnis):

1. **Memory-Operatoren-Skill** — Lese-Vertrag, die drei Einzelakte (`topic create`, `package commit`, `package reject`), je-Eintrag-Review, Aufsetz-Ritual, Fehlerverhalten.
2. **Task-Definition-Skill** — Delegations-Disziplin: strukturierte Argumente, Einzeiler-Regel, Protokoll/Deliverable-Trennung.

Das Agent-Grundset (SYSTEM.md) bleibt vollständig user-managed — das Produkt schreibt es nie an. Der Installer bietet eine **optionale Vorlage** an: nur wenn keine existiert, laut gefragt, nie still überschrieben.

## 9. Verdrahtung

**Verdichter = Worker-Rolle:** `curator` in `[roles.curator]`; Verdichtungs-Läufe laufen als Tasks auf derselben Queue mit derselben Infrastruktur wie alle anderen Aufgaben — kein Sonderpfad. Der Curator liest Digests und **baut Pakete** (Staging) — write-frei am Sockel, nie committend (Auto-Commit ausgenommen, §5).

**Dispatch:** `[dispatch]` steuert die Kopplung — je Zeile Session-Typ → Verdichtungs-Lauf (Profil: topic/domain) + Auto-Verhalten (ja/nein).

**Trigger (hooks/, Infrastruktur in Code — keine Agent-Arbeit in der Session):**
- **Session-End-Hook:** von pi gerufen, wenn eine Session endet; baut den Digest aus dem Session-Protokoll und enqueue-t die Verdichtungs-Tasks je Dispatch-Zeile (in der Regel 2: topic + domain).
- **Nacht-Sweep:** systemd-User-Timer ruft `pib digest sweep` — offene Sessions nachverdichten + Auto-Commits für `auto=ja`-Zeilen (Provenienz „auto").

**Idempotenz:** Doppelverdichtung ausgeschlossen — das `digest/`-Fach vermerkt verarbeitete Sessions (queue → done/failed); Läufe sind idempotent; ein **änderungsloser Lauf ist ein vollwertiges Ergebnis**.

## 10. Installer & Clean-Slate

`install.sh` — idempotent, laut, nichts still überschreiben:

1. Root anlegen: `~/.pi/pi-bundle/` mit `memory/`, `worker/`, `lib/`, `hooks/`
2. `lib/` aus dem geklonten Repo installieren (das Repo ist die Quelle)
3. Konfig-Skelett: `config.toml` mit kommentierten Platzhaltern — Instanz-Daten füllt der User, der Installer ratet nichts
4. `pib` auf PATH (Symlink `~/.local/bin/pib`)
5. Skills in pis Skill-Verzeichnis registrieren
6. Session-End-Hook registrieren + systemd-User-Service (Watchdog) und -Timer (Nacht-Sweep) anlegen
7. Optionale SYSTEM.md-Vorlage anbieten — nur wenn keine existiert, laut gefragt
8. Abschluss: `pib doctor` — Struktur + Konfig laut geprüft, fehlende Instanz-Daten gemeldet

Der Installer erfindet keine Instanz-Daten, überschreibt keine bestehenden Dateien (Konflikt → laut abbrechen) und greift außer dem Repo-Klonen nicht ins Netz.

**Keine Migration bestehender Daten** — Clean-Slate je Installation; ein Umzug bestehender Wissensstände wäre ein separates Vorhaben.

**Clean-Slate-Test (lurch):** nacktes pi vorausgesetzt, sonst nichts. Ablauf: Repo klonen → `install.sh` → Konfig ausfüllen → `pib doctor` grün → **Smoke-Test über den echten Prozess:** Topic anlegen, Paket-Loop (Vorschlag → Freigabe → Commit), Task-Delegation mit Protokoll, Verdichtungs-Lauf. Der Test beweist: läuft ab nackter pi-Installation ohne Vorprojekte.

## 11. Doku-Satz

| Artefakt | Zweck |
|---|---|
| `README.md` | Einstieg + Quick-Start (Klon → `install.sh` → Konfig ausfüllen → `pib doctor` → Smoke-Test) |
| `ANFORDERUNGEN.md` | Anforderungen v1.0 (Basis) |
| `CONCEPT.md` | dieses Dokument |
| `skills/` | Agent-seitige Verhaltens-Doku |
| `config.toml`-Skelett | Instanz-Daten, kommentiert, selbst-dokumentierend |

**Regel:** keine Historie-Referenzen — die Docs nennen keine Vorgänger-Systeme als Voraussetzung. Die Agent-Rituale leben in den Skills, die Instanz-Erklärung im Konfig-Skelett — keine doppelte Doku.

## Anhang: Anforderungs-Deckung

| Anforderung | Umsetzung |
|---|---|
| F1 gepflegter Wissensstand | §5 Sockel |
| F2 Herkunft & Snapshots | §5 provenienz/snapshots |
| F3 kontrollierter Schreibkanal | §5 Staging → Freigabe → Commit |
| F4 nachgelagerte Verdichtung | §9 Trigger + Idempotenz |
| F5 Domänen-Wissen | §5 Domänen |
| F6 Index als Spiegel | §5 Index |
| F7 Spielarten | §5 sockel.md-Abschnitte |
| F8 Auto-Commit Low-Stakes | §5/§9 auto=ja |
| F9 Dialog responsiv | §6 Queue serial |
| F10 Tasks nur Inhalte | §6 Task-Format |
| F11 Rollen definieren Rechte | §6 Rollen-Setup |
| F12 Modellwahl & Entladen | §6/§7 konfigurierbar je Instanz |
| F13 Cloud nur auf Entscheidung | §6 keine automatische Auswahl |
| F14 Protokoll je Task | §6 Protokoll-Trennung |
| F15 Timer-Tasks | §6 derselbe Pfad |
| F16 Fehler laut, kein Auto-Retry | §6 Fehler |
| F17 Verdichter = Worker-Rolle | §9 curator |
| F18 Dispatch-Konfiguration | §9 [dispatch] |
| F19 Trigger in Code | §9 hooks/ |
| F20 konsistentes Bedienungsmodell | §3/§4 ein Root, eine Konfig, ein CLI |
| NF1 ab nackter pi-Installation | §10 Clean-Slate |
| NF2 generic | §2/§3 Instanz-Daten je Installation |
| NF3 einfach einzurichten | §10 install.sh + lurch |
| NF4 Doku ohne Vorwissen | §11 Doku-Satz |
| NF5 Menschenlesbar & backup-freundlich | §2 Dateien, ein Root |
| NF6 keine stillen Zustände | §5/§6/§7 laut überall |
| NF7 Secrets außerhalb | §2/§3 pi-übliche Orte |
| NF8 Open Source | öffentliches Repo |
| F21 (Ergänzung) Struktur-/Konfig-Prüfung | §4 pib doctor |
