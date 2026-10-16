# SKIZZE — Memory-Föderation Lurch ↔ Igor (geteilte Basis per NFS)

**Status:** Idee/Skizze, nicht beschlossen · **Stand:** 2026-10-16 · **Kontext:** pi-bundle-Austestphase; Igor migriert in den kommenden Wochen von pi-brain/pi-worker auf pi-bundle.

## Ziel

Igor und Lurch sind im LAN gleichberechtigt. An einem Topic wird **sequenziell** gearbeitet (nie zeitgleich). Jeder Agent soll in seiner nächsten Session das **vollständige Topic-Wissen** haben — auch die Anteile, die der andere Agent beigetragen hat, inklusive Herleitung (Provenienz bis zur Original-Session des anderen).

## Grundprinzip

Die Memory-Basis wird als **gemeinsamer NFS-Share** geführt (NFSv4, LAN-only, enge Export-/Rechtepolitik). Keine Mirror-/Whitelist-Abstraktion — Vollteilung des Zustands.

## Artefakt-Matrix

| Artefakt | geteilt? | Begründung / Regel |
|---|---|---|
| `sockel.md`, `provenienz.md` | ja | Der committete Zustand — Kern der Föderation. |
| `snapshots/` | ja | Append-only, Namen mit Session-UUID → kollisionsfrei. Gemeinsamer Audit-Trail; Stelle-4-Rückblicke beider Agenten rekonstruierbar. |
| `staging/<topic>/`, `staging/_domains/` | ja | Paketnamen tragen Session-UUID → kollisionsfrei. **Nebeneffekt:** Engine-Dedup („gegen offenes Staging desselben Fachs") wirkt automatisch agentenübergreifend → Doppelvorschläge strukturell unmöglich. |
| `digest/queue/` | ja, **geteilte Queue** | Produzenten (Session-End-Hooks) schreiben nur hinein (UUID-Namen, konfliktfrei). Einziger Konsument ist der Sweep. |
| Session-Logs | ja | Macht Provenienz grenzüberschreitend auflösbar. S6-Grenze: Secrets in Logs → Share-Zugriff strikt begrenzen. |
| `INDEX.md` | ja | Spiegel-Sync; nur unter Commit-Lock sicher (s. u.). |

## Betriebsmodell

- **Curator je Host** (kein Zentral-Curator): Ausfallsicherheit — ist ein Agent down, bleibt der andere voll arbeitsfähig inkl. Curator/Sweep.
- **Sweep als einziger Queue-Konsument:** Der Sweep eines Hosts darf die Queue leeren, wenn der andere Host down ist (Notfall-Vertretung, auch für dessen Digest-Rückstände).
- **Mutual Exclusion der Sweeps:** Beide Sweeps akzeptieren ein gemeinsames `flock` (z. B. `memory/.sweep.lock`), nicht-blockierend; der Zweite bricht laut ab („Sweep aktiv, übersprungen"). Zeitversetzte Timer (02:00 / 02:30) sind Höflichkeit, das Lock ist die harte Regel (Clock-Drift, Nachhole-Läufe, Downtime).
- **Sequenz-Modell:** „Nie zwei Commits gleichzeitig" wird organisch (sequenzielle Topic-Arbeit) plus Lock abgesichert.
- **Freigabe bleibt heilig und lokal:** `[a]`/`[b]` je Eintrag, je Agent. Jeder committet nur, was er selbst freigegeben hat.

## Mechanik-Eingriffe in der Engine (Minimalset)

1. **`flock` um den Commit-Kern** in `pib.py` (Validierung Phase 2 → atomarer Swap → Provenienz/Snapshot/INDEX): die Read-Modify-Write-Folge auf `sockel.md`/`INDEX.md` ist heute ungeschützt; `os.replace` allein schützt die Sequenz nicht.
2. **Sweep-Lock** wie oben (Produzent/Konsument-Trennung der Queue).
3. **Dispatch-Matrizen konfiguriert identisch:** Verdichtet Host A den Digest von Host B, gelten A's `[dispatch]`-Zeilen. Die Matrizen sind Teil der Föderations-Verabredung und bewusst gleichzuhalten.
4. **Provenienz:** Einträge belegen die `session_id` des Ursprungsagenten, auch wenn der fremde Curator das Paket baute — auflösbar über die geteilten Session-Logs.

## Sicherheit (S6)

- NFS-Export nur ins LAN, Rechte 0750, keine Secrets in der geteilten Basis.
- Session-Logs enthalten Roh-Prompts → Zugriffsliste bewusst klein halten.

## Offene Punkte

- Share-Location und Export-Politik (NAS? backupserver? LXC?)
- NFSv4-Verfügbarkeit/`lockd` auf beiden Seiten verifizieren (NFSv3 ohne lockd → flock unzuverlässig)
- Zugriffsmodell für Session-Logs (S6)
- Migrationspfad für Igors Bestandsdaten (SUMMARY/FACTS/TIMELINE → Sockel) — Konvertierung vermutlich im Zuge seiner pi-bundle-Migration
- Verhalten zweier `config.toml` bei künftigen Divergenzen (Modell-Aliase, Trigger)

## Nicht Ziel (bewusst)

- Kein zeitgleiches Schreiben an einem Topic
- Kein Zentral-Curator (abgelehnt: gemeinsame kritische Komponente)
- Keine host-seitig getrennte digest-Queue (abgelehnt: bricht die Symmetrie)
- Keine Mirror-/Publish-Whitelist-Stufe (überschrieben durch Vollteilungs-Ansatz)
