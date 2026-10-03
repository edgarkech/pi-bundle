# ANFORDERUNGEN — pi-bundle

**Status:** v1.0 (2026-10-03) — **festgezurrt**; Ergänzungen weiterhin möglich (§6)
**Home (SSoT):** `~/github-projects/pi-bundle/` · **Zielrepo:** öffentlich auf GitHub (`edgarkech`)
**Kein Design:** Dieses Dokument enthält bewusst keine Design-Entscheidungen (Pfade, Konfig-Format, CLI, Verzeichnisstruktur). Design wird abgeleitet aus festgezurrten Anforderungen — nicht vorweggenommen.

## 1. Warum dieses Produkt (Motivation)

Ein KI-Agent, der über Sessions hinweg an Vorhaben arbeitet, scheitert ohne Hilfsapparatur an vier Grundproblemen:

**P1 — Verlust des Wissensstands.** Sessions enden, Kontextfenster sind begrenzt. Ohne strukturiertes Gedächtnis startet jede Session bei null oder arbeitet auf einem Nachlass alter Notizen — ohne zu wissen, was davon noch gilt. Das Vorhaben verliert seinen roten Faden.

**P2 — Fehlende Nachvollziehbarkeit.** Wurde eine Entscheidung vor drei Wochen getroffen — mit welcher Begründung? Ohne Herkunftsverfolgung bleibt die Antwort „ich erinnere mich nicht"; und eine falsche Erinnerung ist gefährlicher als keine.

**P3 — Der Dialog blockiert.** Recherche, Code-Arbeit, System-Aufgaben: Was lange dauert oder viele Schritte braucht, macht den Agent im Dialog unbrauchbar, wenn es synchron läuft.

**P4 — Autonomie ohne Kontrolle.** Ein System, das Wissen schreibt und Aufgaben ausführt, braucht Kontrollleisten: Der Mensch muss an den entscheidenden Stellen zustimmen, Fehlverhalten muss laut sichtbar sein statt still ersetzt zu werden.

**Die Kopplung ist das eigentliche Problem.** Für die Einzelprobleme existieren Lösungen — Memory-Systeme und Worker-Architekturen. Wer beides zusammen betreibt, verdrahtet zwei Apparaturen: zwei Konfigurationsquellen, zwei Pfadkonventionen, zwei Bedienungsmodelle, trigger-übergreifende Abläufe. Diese Kopplung erzeugt den eigentlichen Einrichtungs- und Betriebsaufwand — und macht das Gesamtsystem für Dritte praktisch unnachbaubar.

**Das Produkt löst das, indem es die Kopplung zum Kern macht:** Memory und Worker als **eine Einheit** — in einem Rutsch eingerichtet (ein Installer), konsistent bedient (eine zentrale Konfiguration, ein Prozessmodell), selbsttragend dokumentiert (verständlich ohne Vorwissen über Einzelkomponenten).

## 2. Grundideen

1. **Prozess vor Technik.** Das Memory wird aus dem Prozess gebaut, den User und Agent an einem Vorhaben durchlaufen (Aufsetzen → Wiedereinstieg → Facharbeit → Sichern → Rückblick → Umplanen). Jede Information hat genau dort ihren Ort, wo sie im Prozess gebraucht wird — nicht dort, wo die Technik sie ablegen möchte.
2. **Der gepflegte Stand, nicht der Nachlass.** Der Agent arbeitet auf einem aktuell freigegebenen Wissensstand. Sessions produzieren Wissen, sind aber nie Kontext.
3. **Ein kontrollierter Schreibkanal.** Wissen ändert sich nur durch: Vorschlag (Paket) → Freigabe je Eintrag → atomarer Commit. Der Mensch ist die einzige Schreib-Entscheidungsinstanz.
4. **Mechanik vom Denken getrennt.** Der Agent im Dialog trägt keine Memory-Apparatur im Kopf; Staging, Paketbau und Commit-Mechanik leben in Code und im asynchronen Verdichter.
5. **Asynchrone Delegation mit Rollen.** Schwere Aufgaben laufen als Tasks mit rollenspezifischen Rechten und Wissensquellen; der Dialog bleibt frei.
6. **Determinismus statt stiller Ersetzung.** Validierung vor Schreiben, atomare Operationen, klare Fehlerklassen — kein stiller Fallback, der Konfigurationsdrift maskiert.
7. **Lokal-first.** Alles Deterministische läuft in Code; der normale Lastpfad ist lokal. Externe Provider (Cloud) sind die konfigurierbare Ausnahme, nicht die Voraussetzung.
8. **Ein Produkt, nicht zwei Systeme.** Memory und Worker teilen sich eine zentrale Konfiguration, eine Struktur, ein Bedienungsmodell — die Kopplung ist der Kern, nicht ein Beiwerk.

## 3. Grundannahmen

Voraussetzungen, die das Produkt nutzt, aber nicht selbst definiert:

1. **pi-Installation** — der Agent läuft auf pi (Sessions, Skills, Tool-Set). Einzige harte Voraussetzung.
2. **SYSTEM.md als Agent-Grundset** — pi lädt SYSTEM.md in jeder Session als immer geladenen Kontext. Das Produkt nutzt ihn, um dem Agent **klare Verhaltensregeln vorzugeben** (Prozessrollen, Schreibkanal-Disziplin, Ausführungs-Regeln) — Disziplinierung über Regeln im Kontext, nicht über Apparatur. Gepflegt manuell beim User; das System erzeugt dafür keine Vorschläge.
3. **Maschinenlesbare Session-Protokolle** — Arbeitssessions erzeugen Protokolle, die eine Session technisch vollständig dokumentieren. Sie sind Quelle der nachgelagerten Verdichtung.
4. **Konfigurierbares LLM-Backend** — lokal (Router-Server) oder extern (Cloud), austauschbar über Konfiguration. Das Produkt hat kein hartes Cloud-Erfordernis: alles Deterministische läuft in Code, damit bleibt es auch auf strikt lokalen Maschinen voll funktionsfähig.
5. **File-basierter Träger** — Menschenlesbarkeit, einfache manuelle Korrektur, einfaches Backup; bewusst statt einer Datenbank.
6. **Ein User als Eigentümer** — der User ist die einzige Entscheidungsinstanz; das System erzeugt keine stillen Zustandsänderungen.

## 4. Anforderungen

Alle Anforderungen auf derselben Ebene: *was* das Produkt leisten muss. Das *wie* (Träger, Formate, CLI, Pfade) ist Design.

### 4.1 Memory

| # | Anforderung |
|---|---|
| F1 | Je Vorhaben (Topic) einen gepflegten Wissensstand, aus dem eine frische Session **ohne Vorgänger-Session und ohne Archiv-Zugriff** voll arbeitsfähig ist |
| F2 | **Nachvollziehbarkeit:** Herkunft (Datum, Session) je Eintrag, auf Zuruf ladbar; zurückliegende Zustände als Snapshots abrufbar — Rückblick ohne eigene Verlaufs-Datei |
| F3 | **Kontrollierter Schreibkanal:** Änderung nur als Paketvorschlag → Freigabe je Eintrag (oder Paket auf einmal) → atomarer Commit; kein Schreiben ohne Freigabe. Einzige Ausnahme: die Anlage eines neuen Topics ist die User-Entscheidung selbst |
| F4 | **Nachgelagerte Verdichtung:** Session-Ende + nächtlicher Sweep triggern einen Verdichter (asynchron), der aus dem Session-Protokoll Freigabe-Vorschläge baut; Doppelverdichtung ist ausgeschlossen; ein änderungsloser Lauf ist ein vollwertiges Ergebnis |
| F5 | **Domänen-Wissen** für nicht-gebundenes Wissen jenseits einzelner Vorhaben — gleich gepflegt (Engine) und manuell korrigierbar |
| F6 | **Index als Spiegel** des Bestands (Vorhaben + Domänen), fortgeschrieben im selben Schreibakt wie die Quelle, nie selbst Quelle |
| F7 | **Spielarten:** verschiedene Vorhaben-Arten brauchen verschiedene Tiefen (Vollausprägung „Projekt" bis abgespeckte Chat-Themen); fehlende Bereiche sind Platzhalter, keine Fehler |
| F8 | **Auto-Commit für Low-Stakes-Fälle** (definierte abgespeckte Themen) — konfigurierbar pro Thema, Herkunft wird dokumentiert; Standard bleibt Freigabe durch den User |

### 4.2 Worker

| # | Anforderung |
|---|---|
| F9 | **Dialog bleibt responsiv:** schwere Aufgaben laufen asynchron in einem dedizierten Worker-Prozess — Single Worker, serielle Abarbeitung, file-basierte Queue |
| F10 | **Tasks tragen nur Inhalte:** Format, Prompt-Bau und Validierung erledigt die Mechanik (Code) — deterministisch, atomar ins Queue-Staging |
| F11 | **Rollen definieren Rechte und Wissensquellen** — Tools kommen nie aus dem Task |
| F12 | **Modellwahl pro Task/Rolle** aus der zentralen Konfiguration; das Task-Modell wird vor dem Run deterministisch geladen; das Entlade-Verhalten ist **konfigurierbar je Instanz** (z. B. strikt entladen bei Maschinen mit Wärme-/Lüfter-Problematik, Keep-Warm sonst). Kein stiller Fallback |
| F13 | **Externe Provider (Cloud) nur auf ausdrückliche User-Entscheidung** |
| F14 | **Protokoll je Task** (Ergebnis, Trace, Reflexion), mechanisch geprüft; Deliverables landen ausschließlich in deklarierten Pfaden, nie im Protokollbereich |
| F15 | **Timer-gesteuerte Tasks** laufen über denselben Pfad wie manuelle Tasks (kein Sonderweg) |
| F16 | **Fehler werden klassifiziert und laut gemeldet** — kein Auto-Retry, der User entscheidet |

### 4.3 Verdrahtung (die Kopplung als Kern)

| # | Anforderung |
|---|---|
| F17 | **Der Verdichter ist eine Worker-Rolle:** Memory-Verdichtung läuft als Task auf derselben Worker-Infrastruktur wie alle anderen Aufgaben |
| F18 | **Dispatch-Konfiguration steuert die Verdrahtung:** welche Session-Typen welche Verdichtungs-Läufe auslösen (Profil, Auto-Verhalten) |
| F19 | **Trigger sind Infrastruktur in Code** (Session-Ende, Nacht-Sweep) — keine Agent-Arbeit in der Session |
| F20 | **Ein konsistentes Bedienungsmodell** über beide Komponenten: eine zentrale Konfiguration, eine Struktur, eine CLI (konkrete Ausgestaltung = Design, abgeleitet aus diesen Anforderungen) |

### 4.4 Nicht-funktional

| # | Anforderung |
|---|---|
| NF1 | **Selbsttragend:** läuft ab nackter pi-Installation; keine Abhängigkeit von Vorprojekten oder bestehender Memory-Struktur |
| NF2 | **Generic:** Code und Konfig-Schema maschinenunabhängig; instanzspezifische Daten (Modell-Aliase, Pfade, Räume) entstehen je Installation |
| NF3 | **Einfach einzurichten:** ein Installer legt Struktur und Konfig-Skelett an; Clean-Slate-Test auf einer frischen Maschine (erste Test-Maschine: lurch) |
| NF4 | **Doku ohne Vorwissen:** das Produkt versteht sich aus seinen eigenen Dokumenten — keine Kenntnis von Einzelkomponenten-Historie nötig |
| NF5 | **Menschenlesbar & backup-freundlich:** Träger, Queue, Konfiguration als Dateien |
| NF6 | **Keine stillen Zustände:** jeder Vorgang meldet laut oder lässt den Zustand unverändert; ein halber Commit existiert nicht |
| NF7 | **Secrets außerhalb:** Keys/Tokens an pi-üblichen Orten, nie in Konfiguration, Backup oder Logs |
| NF8 | **Open Source:** öffentliches Repo |

## 5. Abgrenzung (bewusst nicht Zielseite)

| Abgrenzung | Begründung |
|---|---|
| Kein paralleler Worker-Cluster (serial, max. 1 laufender Task) | Einfachheit vor Durchsatz; Single-Instanz ist bewusstes Design |
| Kein Auto-Retry, keine automatische Modell-/Cloud-Auswahl | User bleibt im Loop; stille Ersetzung maskiert Fehler |
| Keine API-Schnittstelle nach außen | interne, user-managed Pipeline |
| Keine Datenbank für Memory | Menschenlesbarkeit, manuelle Korrektur, einfaches Backup |
| Keine Verlaufs-Datei im Memory | Verlauf = geladener Blick in Snapshots + Herkunft, kein Zustands-Anhang |
| Keine System-Vorschläge aus dem System | das Agent-Grundset bleibt manuell beim User |

## 6. Offen zur Ergänzung durch Edgar

1. **Fehlende Probleme oder Grundideen?** (§1/§2) — deckt die Motivation das Produkt vollständig ab? (Ergänzungen, sobald etwas einfällt)
2. **Fehlende Anforderungen?** Was muss das Produkt leisten, was hier nicht steht?
3. **Paritäts-Basislinie:** F1–F20 + NF1–NF8 beschreiben die Union der beiden Referenzprodukte (pi-brain, pi-worker) plus Verdrahtung — alles Muss, keine Auswahl. Ein Teilmengen-Bundle widerspräche dem Produkt. Abweichungen nur als explizite, begründete Ausnahme.

**Beantwortet in v0.3:** Entlade-Verhalten konfigurierbar statt strikt (F12) · SYSTEM.md als Agent-Grundset als Grundannahme dokumentiert (§3.2)
