---
name: memory-operatoren
description: "Operatoren-Schnittstelle zur pi-bundle Memory-Engine (CLI pib): Lese-Vertrag `pib topic read` (Sockel + Staging-Block + on-demand Provenienz/Snapshot), vier gestattete Einzelakte (`pib topic create`, `pib domain create`, `pib package commit`, `pib package reject`), je-Eintrag-Review mit Teil-Commits, Domänen-Pakete über das `_domains`-Fach, Fehlerverhalten. Nutzen, wenn: das Gespräch zu einem Memory-Topic wechselt (Aufsetz-Ritual), Einträge committet/verworfen werden (Freigabe/Verwerfung), Topics oder Domänen angelegt werden, oder Fragen zur Engine-Bedienung anstehen."
---

# Skill: memory-operatoren

**Beschreibung:** Operatoren-Doku der pi-bundle Memory-Engine. Regelt, wie dieser Agent die Engine über die CLI `pib` bedient: Lesen, vier gestattete Einzelakte, je-Eintrag-Review, Aufsetz-Ritual.

**Kern-Prinzip (Rollen-Modell):**
- **Der Verdichter (Rolle `curator`) baut Pakete** (Staging-Papier) — write-frei auf dem Sockel, nie committend (Auto-Commit ausgenommen: nur für Dispatch-Zeilen `auto=ja`, angestoßen vom Nacht-Sweep, Provenienz „auto").
- **Dieser Agent (Operator) ist mechanik-frei** bis auf vier gestattete Einzelakte: `pib topic create` · `pib domain create` · `pib package commit` · `pib package reject`. Lesen (`pib topic read`, `pib pipeline status`) ist frei und kein Mechanik-Kontakt.
- **Der User ist die einzige Entscheidungsinstanz:** Freigabe und Verwerfen **je Eintrag**; ein inhaltlicher Auftrag ersetzt die Freigabe nie.
- **Kein stiller Zustand:** Leere Ausgänge sind Erfolge; Fehler sind laut. Nie still löschen, nie still schreiben.

## 📖 Begriffe (operativ)

| Begriff | Bedeutung |
|---|---|
| **Sockel** | Der Ziel-Zustand eines Topics: `topics/<name>/sockel.md` (EINE Datei, Abschnitte: Steckbrief/Plan/Journal/Wissen/Verweise) + `provenienz.md` (je Eintrag: id → Abschnitt/Schlüssel/Datum/Session, on-demand) + `snapshots/` (vollständiger Stand je Commit) unter der Memory-Basis. **Keine** SUMMARY/FACTS/TIMELINE. |
| **Paket** | Eine `*.proposal.json` im Staging — Freigabe-Objekt mit Kopf (`paket_typ` topic/domain) und `eintraege[]`, **je Eintrag adressierbar** (`id` e1, e2, …); wird beim Commit wirksam. |
| **Eintrag** | Die Freigabe-Einheit: eine kurze, zustandsbehaftete Aussage mit Beleg (`beleg.turns`/`beleg.artefakt`); `[a]`/`[b]` gelten je Eintrag. Zwei ID-Ebenen: `id` = intra-Paket-Adresse (frei, adressiert im Review); die persistente Topic-ID `eNNNN` (z. B. `e0001`) vergibt die Engine erst beim ersten Commit — im Normal-Read unsichtbar, on-demand in `provenienz.md`. |
| **Staging-Fächer** | `staging/<topic>/` (Topic-Pakete) · `staging/_domains/` (Domänen-Pakete, eigenes Fach) — je mit `done/` (committet) und `rejected/` (verworfen, kalt, dauerhaft). |
| **Domäne** | `domains/<name>.md` für nicht-gebundenes Wissen jenseits einzelner Topics — ein eigener Datentyp mit Meta-Header (`<!-- entry-key: <typ> -->`) und `##`-Entries. Anlage ist ein ausdrücklicher Einzelakt (`pib domain create`), nie still. |

## 🛠️ Technische Basis

- **CLI:** `pib` (implementiert in `lib/pib.py`, Engine in `lib/worker.py` für die Task-Seite). Ein Entry-Point, Grammatik `<objekt> <aktion>`.
- **Bundle-Root:** Env `PI_BUNDLE_HOME` > Flag `--home <PFAD>` je Aufruf > Default `~/.pi/pi-bundle`. **Kein Default-Raten auf Datenpfade:** fehlt die Konfig im Root oder ist der Root nicht auflösbar → lauter Fehler (Exit ≠ 0).
- **Memory-Basis:** aus der zentralen `config.toml` unter `[paths].memory` (relativ zum Root, Default `memory`). Unter ihr liegen die Pfad-Schemata `topics/…` · `domains/…` · `staging/…` · `digest/…` · `INDEX.md`.
- **Konfig-Quelle:** genau eine Datei `config.toml` im Bundle-Root. Für den Operator sind relevant: `[paths]` (memory, sessions_root), `[memory]`, `[dispatch]` (Auto-Commit-Schaltgröße, matrix-blind für die Engine), `[trigger]`.

Aufruf-Praxis: Die Tool-Shell des Operators ist nicht-interaktiv — ein in `~/.profile` exportiertes `PI_BUNDLE_HOME` kann unwirksam sein. Deshalb **bei Bedarf `--home <absoluter Bundle-Root>` je Aufruf mitgeben** oder `PI_BUNDLE_HOME` explizit setzen; ohne auflösbaren Root schlägt der Aufruf laut fehl (kein Default).

## 🚀 Befehle & Syntax

Alle Befehle über das CLI:
```bash
pib [--home <BUNDLE_ROOT>] <objekt> <aktion> ...
```

### 1. `pib topic read --topic <name> [--provenienz] [--snapshot <ts>-<sid>]` — Lese-Vertrag (frei, kein Einzelakt)
- **Normal-Read:** `sockel.md` (IDs/Comments gestrippt — menschenlesbarer Stand) + **Staging-Block**: alle offenen Topic-Pakete des Topics, je Paket Kopf + `eintraege[]` vollständig (id, typ, operation, abschnitt, schluessel, text, beleg — inkl. operationsspezifischer Payload-Felder). Kein Vorfiltern, kein Auslassen; leeres Staging explizit (leere Liste).
- **`--provenienz`:** lädt on-demand `provenienz.md` (Tabelle id → abschnitt/schluessel/datum/session). Nicht im Normal-Read (Invariante: Herkunft on-demand).
- **`--snapshot <ts>-<sid>`:** lädt den Snapshot statt des Live-Stands (Stelle-4-Rückblick zum Entscheidungszeitpunkt) inkl. Snapshot-Liste. Ungültiger Name/fehlender Snapshot → lauter Fehler.
- **Domänen-Pakete sind kein read-Ziel** — ihr Lese-/Review-Pfad läuft über die Fächer (siehe Workflow).

### 2. `pib topic create --name <n> --titel <t> --spielart <projekt|dauer_betrieb|einzel|austausch> [--projekt <abs>] [--beschreibung <d>]` — Einzelakt 1
- **Voraussetzung:** User-Entscheidung zur Anlage — nie selbstständig.
- Sockel-Skelett nach der Spielart-Matrix (`austausch`: abgespeckt, **kein `plan`**). **Kein Auto-Commit-Flag** — die Schaltgröße ist die Dispatch-Matrix (`config.toml` `[dispatch]`, Sweep-seitig; die Engine ist matrix-blind).
- Namens-Kollision → Fehler (Idempotenz: nein; `projekt` nur für `projekt`/`austausch`).

### 3. `pib domain create --name <n> --titel <t> --entry-key <k>` — Einzelakt 2
- **Voraussetzung:** ausdrückliche User-Entscheidung zur Domänen-Anlage — **nie still** (die Zieldatei muss existieren, bevor Domänen-Pakete darauf committen).
- Legt `domains/<name>.md` mit Meta-Header (`<!-- entry-key: <k> -->` als erste Zeile) + Titelzeile an.
- Namens-Kollision → Fehler (Idempotenz: nein).

### 4. `pib package commit --paket <abs> [--eintraege e1 --eintraege e3]` — Einzelakt 3
- **Voraussetzung:** explizite Freigabe **je Eintrag** am präsentierten Paket; `--eintraege` ist ein **wiederholbares Flag, je eine ID** (keine Komma-Liste).
- **Ohne `--eintraege`:** ganzes Paket wird committet (Kurzform — „auf einmal freigegeben"; nur als explizite Freigabe des ganzen Pakets).
- **Mit:** nur die freigegebene Teilmenge — **Teil-Commit = Normalfall**.
- Ablauf: Two-Phase-Validierung (Struktur des vollen Pakets → Zustand des freigegebenen Subsets, sequenziell) → atomarer Swap → Folgeregeln (Provenienz je angewandtem Eintrag + Snapshot des vollständigen Stands + INDEX-Spiegel-Sync im selben Akt). Angewandte Einträge werden aus der Paket-Datei entfernt; Paket danach leer → `done/`, sonst bleibt es offen mit den verbleibenden (weiter adressierbaren) Einträgen.
- Wirkt auf beide Pakettypen (Topic- und Domänen-Pakete); Auto-Commit (`[dispatch]`-Zeile `auto=ja`, Provenienz „auto") läuft über denselben Codepfad, angestoßen vom Nacht-Sweep — der Operator committet nie automatisch.

### 5. `pib package reject --paket <abs> [--eintraege e1 --eintraege e2] [--grund <text>]` — Einzelakt 4
- **Voraussetzung:** User-Entscheidung zur Verwerfung — die Verwerfung ist selbst die Entscheidung, kein zusätzlicher Freigabe-Akt.
- Verworfene Einträge wandern **dokumentiert** nach `rejected/` im Fach des Pakets (Kopf + Einträge + `grund` + `datum`; kalt, keine Auswertungsquelle; erneute Vorschläge über Sessions hinweg zulässig); aus der offenen Paket-Datei entfernt; Paket danach leer → `done/`. **Kein Snapshot bei Reject** (Stand ändert sich nicht).
- Bewusst **keine semantische Validierung**: auch ein defektes Paket muss verwerfbar sein (Routing nur nach `paket_typ`/`ziel`).

### 6. `pib pipeline status` — Pipeline-Rückstände (rein lesend, kein Einzelakt)
- Liefert `queue` (offene Digests in `digest/queue/`) · `failed` (Eskalations-Zustände in `digest/failed/`) · `offene_sessions` (noch nicht verdichtete Session-Dateien). Das Aufsetz-Ritual meldet sie als Zähler — Details auf Zuruf.

**Kein weiterer Mechanik-Kontakt.** Kein direktes Schreiben auf Sockel, Domänen-Dateien oder INDEX — alles läuft über Pakete + die vier Einzelakte.

## 🔄 Workflow — Aufsetz-Ritual

1. **LADEN:** `pib topic read --topic <name>` (Sockel + Staging-Block). Topic fehlt → Schritt 2.
2. **Topic-Anlage?** Hinweis an den User; **er** entscheidet: Anlage (`pib topic create`, Einzelakt 1) · anderes Topic · weiter ohne Topic.
3. **PRÄSENTIEREN:** Ausstehende Topic-Pakete **vollständig** (read-Staging, je Eintrag adressierbar) **+ ausstehende Domänen-Pakete** (`staging/_domains/*.proposal.json`, direkt lesbar — kein read-Ziel). Pipeline-Rückstände als Zähler (`pib pipeline status`) melden — Details auf Zuruf.
4. **REVIEW je Eintrag:** `[a]` (freigeben → `pib package commit` mit den freigegebenen IDs) / `[b]` (verwerfen → `pib package reject`) — je Eintrag einzeln; „auf einmal" nur als explizite Freigabe des ganzen Pakets. **Ausgenommen: Dispatch-Zeilen `auto=ja`** (z. B. Low-Stakes-Gruppenraum) — deren Pakete werden ohne Ritual auto-committet (Nacht-Sweep, Provenienz „auto"); Zeilen `auto=nein` laufen Freigabe je Eintrag.
5. **ARBEIT:** Substanzielle Signale (Entscheidung, Festzurrung, Fund, Korrektur) → keine Nebenaufgabe im Dialog; die Proposition läuft vollständig nachgelagert über die Verdichtung (Session-Digest → Curator → Paket → Freigabe je Eintrag).
6. **SESSION-ENDE:** Keine Memory-Aktion — die Verdichtungs-Pipeline läuft automatisch (Session-End-Hook → Digest → curator-Läufe je `[dispatch]`-Zeile; in der Regel 2: topic + domain). Nächste Session präsentiert die daraus entstandenen Pakete.

## Review-Optionen je Eintrag

| Option | Ablauf | Ausführung |
|---|---|---|
| **Commit** `[a]` | Explizite Freigabe **je Eintrag** (oder des ganzen Pakets als Kurzform) | `pib package commit --paket <abs> [--eintraege <ids>]` → Echo melden; Paket leer → `done/` |
| **Reject** `[b]` | User-Entscheidung am Eintrag (oder am ganzen Paket) | `pib package reject --paket <abs> [--eintraege <ids>] [--grund]` → verworfen dokumentiert in `rejected/`; Paket leer → `done/` |

**Regeln:**
- **Ein inhaltlicher Auftrag ersetzt die Freigabe nicht** — auch nach einer Umformulierung wird erst auf explizites `[a]` committet.
- **Teil-Commit = Normalfall:** mehrere Einträge desselben Pakets werden je entschieden; ein Commit pro Review-Runde mit den freigegebenen IDs ist zulässig.
- **Kein Paket-Papier-Edit durch den Operator** — das Staging-Papier wird nie direkt angefasst; Korrekturbedarf geht als `[b]` (verpasste Inhalte kommen über die nachgelagerte Verdichtung erneut) oder als neues `UPSERT` nach dem Commit.
- **Multi-Proposal-Koexistenz:** mehrere Pakete liegen im Staging; exakte Upsert-Duplikate (Eintrag/Domäne) gegen offenes Staging desselben Fachs lehnt die Engine ab (Normalform-Dedup; `SET_FELD`/`REMOVE_EINTRAG`/`REMOVE_DOMAIN_ITEM` sind kein Dedup-Fall).

## Operations-Vokabular (Einträge im Paket)

| Operation | Typ | Payload | Semantik |
|---|---|---|---|
| `SET_FELD` | topic | abschnitt, schluessel(=Feld-ID), Payload je Feldtyp: Skalar → `text` (String) · Liste → `liste` (Liste; ein Einzel-`text` als 1-Element-Liste akzeptiert) | Skalar- oder Liste-Feld setzen (`titel`/`spielart` immutable); `roadmap` nur Spielart `projekt`; austausch-Felder nur `austausch` |
| `UPSERT_EINTRAG` | topic | abschnitt (Subabschnitt: `entscheidungen` · `rahmenbedingungen` · `relationen` · `dokumente` — feste Menge, sonst Validierungsfehler), schluessel, text | Wissen-Eintrag: Match per id oder (abschnitt, normalform(schluessel)) → ersetzen, sonst anhängen. Zustand, kein Verlauf |
| `REMOVE_EINTRAG` | topic | abschnitt, schluessel | Obsoletes wird ersetzt, nicht konserviert. Idempotent: fehlend → no-op |
| `UPSERT_DOMAIN` | domain | ziel (=`domains/x.md`), entry | Entry (`##`-Sektion) anlegen; existiert → no-op. Zieldatei muss existieren |
| `UPSERT_DOMAIN_ITEM` | domain | ziel, entry, text (ohne `item_id` = anhängen, Engine vergibt nächste freie Nummer; mit `item_id` = ersetzen, fehlend → Fehler) | Nummeriertes Item (append-only, Lücken erlaubt, nie neu nummeriert) |
| `REMOVE_DOMAIN_ITEM` | domain | ziel, entry, item_id (=`<entry>-<n>`) | Item entfernen; Lücke bleibt. Idempotent: fehlend → no-op |

**Typ-Konsistenz & Homogenität:** `typ=topic` → nur die ersten drei Ops; `typ=domain` → nur die drei Domain-Ops. Ein Paket trägt nur Einträge eines Typs und `paket_typ` = dieser Typ (Homogenität). **Harte Feldgrenzen** (§2.4) blockieren laut vor jedem Schreibakt.

## Prüfschärfe (Arbeitshilfe; die Entscheidung trifft immer der User)

**Je Eintrag eines Curator-Pakets:**
1. **Zukunftsrelevanz** — handelt eine künftige Session damit anders/richtig?
2. **Zustand, nicht Verlauf** — „was gilt ab jetzt", nicht „was getan wurde"?
3. **Belegbarkeit** — `beleg` (turns/artefakt) vorhanden?
4. **Duplikat-Check gegen den Sockel** — lebt der Inhalt nicht bereits am Heimatort? (Upsert statt Neu?)
5. **Ziel-Struktur** — passender Abschnitt/Subabschnitt, Typ, Fach?

## ⚠️ Fehlerverhalten

- Alle Fehler als klare Ursache: CLI → **Exit-Code ≠ 0** (0 ok · 1 Validierung/I-O · 2 Argumente/Konfig) + strukturierte Meldung auf stderr.
- **Kein stiller Success:** unbekannte Befehle, malformed Payloads, fehlende Ziele, Namens-Kollisionen, Feldgrenzen-Überschreitungen → Fehler **vor** jedem Schreibzugriff.
- **Atomicity (Two-Phase-Commit):** ein einziger Validierungsfehler — der ganze Commit wird verworfen; der Live-Zustand bleibt **byte-identisch**.
- Echo meldet die Ursache **wörtlich** an den User; Fehlermeldung wörtlich weitergeben. **Retry-Budget max. 3 Versuche**, dann Eskalation (Blockade-Format laut, kein stiller Endlos-Retry).

## ⚡ Trigger-Policy (wann dieser Skill anstößt)

- **Themenwechsel/Aufsetzen:** Kontext laden — `pib topic read`; je-Eintrag-Staging-Review (Aufsetz-Ritual, Schritt 3).
- **Freigabe `[a]`** je Eintrag (oder Paket-Kurzform) → `pib package commit`.
- **`[b]`** je Eintrag (oder Paket-Kurzform) → `pib package reject`.
- **Anlage** eines Topics/Domäne (nur nach User-Entscheidung) → `pib topic create` / `pib domain create`.
- **Frage zur Engine-Bedienung** → dieses Skill (nie aus Erinnerung).
- **Session-Ende:** Keine Memory-Aktion — die Verdichtung läuft automatisch.
