---
name: task-definition
description: "Hochgradig deterministische und strukturierte Schnittstelle zur Definition von Worker-Tasks (Delegation) über `pib task create`. Erzwingt strukturierte Argumentübergabe (kein Freitext-Prompt-Formatierung), absolute Pfade, Einzeiler-Regel (Hart), Protokoll/Deliverable-Trennung, Modell-Auflösung und atomares Staging. Pflicht vor JEDER Delegation an eine Worker-Rolle."
---

# Skill: task-definition

**Beschreibung:**
Deterministische Schnittstelle zur Erstellung fehlerfreier Worker-Tasks. Die Syntax-Verantwortung ist **vollständig in der Mechanik** verlagert: `pib task create` baut den Worker-Prompt aus strukturierten fachlichen Argumenten (`--task`, `--spec`, `--deliverable`) — es gibt kein Freitext-Prompt-Formatierungsargument, das der Operator falsch formatieren könnte.

**Kern-Prinzip:**
Der Operator formuliert nur noch den **Inhalt** (Einzeiler + ggf. Konzept-Verweis + Deliverables). Format, Reihenfolge, DELIVERABLE-Zeilen, Modell-Auflösung und Validierung übernimmt `pib task create` **atomar im selben Call** (Exit 0 = direkt `pending`). Kein zweistufiges prepare→approve.

## 🛠️ Technische Basis
- **CLI:** `pib` (Worker-Teil in `lib/worker.py`, verdrahtet in `lib/pib.py`).
- **Bundle-Root:** Env `PI_BUNDLE_HOME` > Flag `--home <PFAD>` > Default `~/.pi/pi-bundle`.
- **Konfig:** zentrale `config.toml` — Sektionen `[worker]` (max_task_chars, default_timeout_seconds, default_model, deliverable_pflicht_roles), `[roles.<name>]` (tools, wissensquellen, model), `[models]` (available = Whitelist, default, cloud_default_model), `[trigger]`.
- **Queue:** `worker/queue/` mit Zuständen `pending → running → completed/failed` — der Zustand ergibt sich aus dem Ordner, nicht aus dem JSON. Serial: ein laufender Task.
- **Protokoll:** `worker/output/<task-id>/` (existiert erst zur Laufzeit).
- **Rollen-Templates/Skills:** liegen unter `skills/` im Repo (Installer registriert sie in pis Skill-Verzeichnis); die Agent-Grund-Doku bleibt user-managed.

## 🚀 Der 3-Stufen-Workflow (verbindlich)

Drei klar getrennte Stufen:

### Stufe 1 – INTAKE
Rolle bestimmen anhand der Aufgabe (gültige Rollen ergeben sich aus den `[roles.*]`-Sektionen der Konfig):

| Aufgabe | Rolle |
|:---|:---|
| Code, Debuggen, Skripte, Tests | `coder` |
| Recherche, Quellenanalyse, Zusammenfassung | `researcher` |
| Architektur, Systemdesign, Trade-offs | `architect` |
| System-Konfiguration, Dienste, Sicherheit | `admin` |
| Memory-Verdichtung aus Session-Digests (write-frei, Paket-Bau) | `curator` |

Der Worker startet kalt und benötigt einen eindeutigen Auftrag — kein Follow-up möglich.

### Stufe 1b – Modellwahl
`--model` optional. Auflösungskette (code-verifiziert): **explizites `--model` > `[roles.<role>].model` > `[worker].default_model`** (fällt `default_model` weg, greift `[models].default`). Das Ergebnis wird **immer konkret** ins Task-JSON geschrieben; der Watchdog kennt keine Modell-Defaults mehr.
- **WORKER-Alias** (z. B. `--model qwen38-27b`): konkreter Alias aus der Whitelist `[models].available` (Validator-Gate: Whitelist ∪ `CLOUD`). Watchdog ruft `pi -p` mit `--model WORKER/<alias>`.
- **CLOUD** (`--model CLOUD`): löst der Watchdog zu `CLOUD/<cloud_default_model>` auf. Für Tasks, die das lokale Kontextfenster sprengen: **CLOUD vorschlagen — die Entscheidung trifft der User (HITL)**, nie selbstständig. Für die Rolle `curator` ist CLOUD **mechanisch gesperrt** (Validator-Sperre; Datenhygiene — Digests enthalten vertrauliche Tool-Outputs, nur lokale Aliase).

### Stufe 2 – FORMULATE (Einzeiler-Politik)
Die **Einzeiler-Regel ist HART**:

> **Wenn du deinen Auftrag an den Worker nicht in `≤ max_task_chars` (Default 250) Zeichen formulieren kannst, erstelle VOR der Taskerzeugung ein minimales Konzept, lege es ins Projektverzeichnis und verweise mittels `--spec` darauf.**

- **`--task`** = der Einzeiler (Deutsch: was soll erreicht werden). **Max `max_task_chars` Zeichen.**
  - Bei Überschreitung: hart abgelehnt (Exit ≠ 0) — verlangt ein Konzept.
  - Für komplexe Aufträge **Phase 0 zuerst**: minimales Konzept ins Projektverzeichnis schreiben, dann nur noch den kurzen Einzeiler + `--spec /abs/pfad`.
- **`--spec`** = optionaler Verweis auf ein Konzept im **Projektverzeichnis** (absolut, **muss existieren**). Der Prompt enthält dann nur noch `Spec: <pfad>` statt dupliziertem Inhalt.
- **`--deliverable`** = optional, **wiederholbar** (ein Attribut je Pfad). Jedes wird in eine eigene `DELIVERABLE:`-Zeile des generierten Prompts gesetzt. **Niemals** kommagetrennt auf einer Zeile übergeben (die Mechanik erlaubt es auch nicht).
- **`--timeout`** = optional, Default `[worker].default_timeout_seconds` (3600), übersteuerbar.

**Regeln (durch die Mechanik erzwungen):**
- `--task` ≤ `max_task_chars` (HARD FAIL sonst).
- `--spec` absolut + existent (kein leerer Verweis).
- `--deliverable` absolut, nicht auf `output/{...}/`, keine Duplikate.
- Alle Pfade absolut (`/...`).

**Formulierungs-Checkliste gegen leeres Ergebnis** (Konvention — nicht mechanisch erzwungen, empirisch belegt):
- Bei Tasks mit Schreibarbeit den Einzeiler um die explizite Protokoll-Formulierung ergänzen, z. B. „Schreibe die Protokolldateien zwingend mit dem write-Tool." (~60 Zeichen — im Budget einplanen). Deliverables: bei **neuen** Dateien `write` vorsehen; bei **Änderungen bestehender** Dateien `edit` (Rolle muss das Tool haben).
- Hintergrund: Der Watchdog verbucht einen Lauf mit rc=0, aber fehlenden Protokoll-Artefakten (`result.md`, `reflection.md`) als `failed`/`empty_result`. Die explizite write-Tool-Formulierung im Einzeiler verhindert das.
- Bei unbekannter Projektstruktur: vollständige Datei-Map direkt in die `--spec` aufnehmen (File-Map-Konvention, s. unten).

### Stufe 3 – VERIFY & HANDOFF (atomar, ein Call)
```bash
pib task create \
  --role coder \
  --task "Unit-Tests für Poller schreiben" \
  --spec /abs/pfad/projekt/docs/concept.md \
  --deliverable /abs/pfad/projekt/tests/poller.test.ts
```
`pib task create`:
1. baut den Prompt **deterministisch** (Einzeiler + ggf. `Spec:` + ggf. `DELIVERABLE:`-Zeilen),
2. validiert intern gegen den Validator (Exit 0 Pflicht),
3. schreibt bei Erfolg **direkt nach `worker/queue/pending/`** (atomar, `status: pending`).

**Exit 0** = handoff-fähig, in der FIFO des Watchdogs.
**Exit ≠ 0** = kein Task erzeugt, klare Fehlermeldung.

> Es gibt **kein** `prepare/`, **kein** `--approve`. Ein Task darf NIE manuell nach `worker/queue/pending/` kopiert/geschrieben werden — nur über das atomare Staging von `pib task create`.

## 🔒 Zwei-Orte-Klarheit (Protokoll ≠ Deliverable) – roter Faden

| Kategorie | Wohin | Zweck |
|:---|:---|:---|
| **PROTOKOLL-Ordner** | `worker/output/<task-id>/` | Worker-Protokolle `result.md`, `reflection.md`, `trace.md` – Audit-Schnittstelle (existiert erst zur Laufzeit). Zusätzlich legt der **Watchdog** automatisch `stdout.log` ab (pi-stdout-Capture, Diagnose bei leerem Ergebnis) – der Worker legt diese Datei **nicht** an. |
| **DELIVERABLE-Ordner** | absolute Ziele aus `--deliverable` | Code, Artefakte, bewertete Ergebnisse (im Projektverzeichnis) |
| **Konzept (Eingabe)** | Projektverzeichnis (absoluter `--spec`-Pfad) | Kontext für den Worker – Verweis statt Duplikat |

**Unabänderlich:**
- Deliverables gehören NIE in den PROTOKOLL-Ordner (und umgekehrt).
- Das **Konzept liegt im Projektverzeichnis**, NICHT in `worker/output/{...}/` – der Output-Ordner existiert zum Zeitpunkt der Taskerzeugung noch gar nicht.
- **`--spec` auf `output/{...}` ist per Konstruktion ausgeschlossen** (dort existiert zum Staging nichts).
- Rollen, die **immer** ein Deliverable-Ziel verlangen (Default: `coder`), sind über `[worker].deliverable_pflicht_roles` konfiguriert — der Validator erzwingt es (mehrere Rollen möglich).

## 🗺️ File-Map-Konvention (empirisch bestätigt)

Bei Tasks mit **unbekannter Projektstruktur** wird die vollständige Datei-Map (Pfadliste der relevanten Dateien) **direkt in die `--spec` aufgenommen** statt Listing-Tools an Rollen zu vergeben (ausdrücklich auch für die Rolle `architect`). Hinweis in der Spec ergänzen: „Die Map ist vollständig — Raten ist unnötig."

Empirie: Blind-Suchen (fehlende Artefakte, ENOENT-Serien) werden eliminiert; alle Modelle finden auch verborgene Artefakte; Laufzeiten und Tool-Calls sinken deutlich.

## ⚡ Trigger-Policy (wann der Workflow anstößt)

* **Jede Task-Delegation** → Skill `task-definition` laden und den 3-Stufen-Workflow durchlaufen. Keine Ausnahme.
* Damit ist der Skill ein **Pflicht-Gate** vor jeder Delegation an eine Worker-Rolle.

## ⚠️ Fehlerverhalten

* `pib task create` liefert bei Regelverletzung **Exit ≠ 0** und eine **eindeutige Fehlermeldung** (auf stderr). Kein Task wird erzeugt.
* Häufige Fehler:
  - „Auftrag zu lang (>max): …" → Einzeiler kürzen ODER Konzept schreiben + `--spec` setzen.
  - „`--spec` zeigt auf nicht existierenden Pfad" → Konzept VOR der Taskerstellung im Projektverzeichnis anlegen.
  - „`--deliverable` zeigt auf output/…" → Deliverable braucht separates Ziel im Projekt.
  - „`--deliverable` nicht absolut" → Pfad mit `/` beginnen.
  - „model nicht in Whitelist" → gültigen Alias (aus `[models].available`) oder `CLOUD` setzen; für `curator` nie `CLOUD`.
* Bei Auftreten: **Fehlermeldung wörtlich analysieren**, Ursache beheben, Call wiederholen.
* **Retry-Budget:** max. 3 Versuche pro Delegation, danach eskalieren (Loop-Prävention). Die präzisen Meldungen sollen einen Treffer beim 1.–2. Versuch ermöglichen.
* **Kein Workaround:** Ein Fail wird nie durch manuelles „Anders-Schreiben" umgangen.

## 🩺 Task-Nachweis auf Anfrage (read-only, kein Polling)

```bash
# Status eines Tasks (read-only über alle Queues)
pib task status <task-id> [--json]

# Ein Task-Journal/Queue-JSON isoliert validieren (Standalone-Diagnose)
pib task validate <task_file>

# Alte completed/failed-Tasks aufräumen (housekeeping)
pib task cleanup [--days <n>] [--dry-run] [--keep-completed]

# Watchdog einmalig (ein Task, dann Exit) — manueller Lauf/Diagnose
pib watchdog --once
```

**Regel:** `pib task status` nur auf **explizite Anforderung des Users**. Kein selbstständiges Pollen, kein `sleep`, keine `ls`/`cat`-Kette zur Standkontrolle — der Watchdog übernimmt die Abarbeitung.

## 📋 Schnell-Referenz (Aufruf-Beispiel)

```bash
# Einfacher Coder-Task mit einem Deliverable
pib task create \
  --role coder \
  --task "Refactore das Backup-Skript nach Python" \
  --deliverable /abs/pfad/projekt/backup.py

# Komplexer Task: Konzept im Projekt + mehrere Deliverables
pib task create \
  --role coder \
  --task "Gateway Daemon-Wiring verdrahten (Konzept s. Spec)" \
  --spec /abs/pfad/projekt/docs/concept_nextcloud.md \
  --deliverable /abs/pfad/projekt/src/config.ts \
  --deliverable /abs/pfad/projekt/src/registry.ts
```

**Generierter Prompt (deterministisch, aus den Argumenten gebaut):**
```text
Gateway Daemon-Wiring verdrahten (Konzept s. Spec)

Spec: /abs/pfad/projekt/docs/concept_nextcloud.md
DELIVERABLE: /abs/pfad/projekt/src/config.ts
DELIVERABLE: /abs/pfad/projekt/src/registry.ts
```
