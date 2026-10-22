---
name: healthchecks-io
description: "Betrieb der healthchecks.io-Totmann-Überwachung per API: Trennung Ping-API (hc-ping.com, kein Key) und Management-API v3 (X-Api-Key), Check anlegen/ändern/auslesen, Grace-Werte in Sekunden, schedule+tz-Präzedenz, Kanäle, Idempotenz via unique, Verifikationsprotokoll, Fehlercodes, Secret-Hygiene. Nutzen, wenn: ein healthchecks.io-Check angelegt, umbenannt, umkonfiguriert (Schedule/Grace/Tags/Kanäle) oder sein Status geprüft werden soll, oder ein Ping-/fail-Ping in einem Skript eingerichtet wird."
---

# Skill: healthchecks-io

**Kurz:** healthchecks.io hat **zwei getrennte APIs**. Fast jeder Fehlversuch entsteht daraus, sie zu verwechseln oder Einheiten/Präzedenzen zu raten. Diese Regeln vor jedem API-Aufruf anwenden.

**Keine SSoT:** Welche Checks existieren und welche Grace-Werte konzeptionell gelten, entscheidet die Repo-Doku (z. B. `network-management/machines/greenfield/greenfield-monitoring-konzept.md`) bzw. das Topic-Memory. Dieses Skill regelt nur die **Mechanik** der API.

## 1. Die zwei APIs (zuerst entscheiden, welche gemeint ist)

| | **Ping-API** | **Management-API v3** |
|---|---|---|
| Zweck | Signal senden: Erfolg / Start / Fail / Log / Exit-Status | Checks anlegen, ändern, auslesen, löschen; Kanäle; Flips; Ping-Historie |
| Basis | `https://hc-ping.com/<uuid>[/<signal>]` | `https://healthchecks.io/api/v3/…` |
| Auth | **keine** — das Schreib-Token ist die UUID in der URL | **`X-Api-Key: <key>`**-Header (Header, **nicht** `Authorization: Bearer`) |
| Erfolg | HTTP 200 + Body `OK` | HTTP 2xx + JSON |
| Typische Falle | Ping auf unbekannte UUID liefert **200 OK** („not found") → ein toter Ping ist am HTTP-Code **nicht** erkennbar | `Bearer`-Header → **401** mit Text „missing api key" |

Signale der Ping-API: `<uuid>` (Erfolg) · `<uuid>/start` · `<uuid>/fail` · `<uuid>/log` · `<uuid>/<exit-status>` (0 = Erfolg). Slug-Form: `https://hc-ping.com/<ping-key>/<slug>[/…]` (Auto-Provisioning nur mit `?create=1`).
Die Altform `hc-ping.com/api/v1/<uuid>/fail` existiert in älteren Skripten und funktioniert; neue Skripte nutzen die kurze Form.

## 2. Secret-Hygiene (gilt ausnahmslos)

- API-Key liegt **ausschließlich** in `~/.healthchecks-credentials` (eine Zeile, roher Wert, Modus `600`). Nie den Wert ausgeben, nie ins Transkript/Log/Repo/Deliverable.
- Aufrufform, die den Wert nicht in die Commandline schreibt: `KEY=$(cat ~/.healthchecks-credentials)` und dann `--header "X-Api-Key: ${KEY}"`.
- **UUIDs und Ping-URLs sind Schreib-Token.** Nicht unnötig ausgeben; API-Antworten auf die benötigten Felder filtern. Bestehende Konvention: Ping-URL steht im Deploy-Skript (private Repos).
- Key **nie** als Commandline-Argument (`--data '{"api_key": …}'` ist zwar offiziell erlaubt, aber sichtbar in `ps` → nicht nutzen).

## 3. Die fünf Fallen, die Zeit gekostet haben

1. **Einheiten sind Sekunden.** `timeout` und `grace`: Minimum 60, Maximum 31536000. Merktabelle: 10 min = `600` · 1 h = `3600` · 2 h = `7200` · 4 h = `14400` · 12 h = `43200`.
2. **`schedule` gewinnt über `timeout`.** Wer beide setzt, bekommt `schedule` gespeichert und `timeout: null` zurück. Cron **und** systemd-`OnCalendar` werden automatisch erkannt; `tz` wirkt **nur** zusammen mit `schedule` (unsere Checks: `Europe/Berlin`).
3. **`schedule` muss zum realen Takt des Jobs passen.** Ein Minutenschedule (`* * * * *`) bei stündlichem Ping lässt den Check dauerhaft in `grace` („late") laufen und streckt die Alarm-Latenz auf `Intervall + grace`. Schedule aus dem echten Cron nehmen (bei `5 * * * *` → `5 * * * *`).
4. **`channels` ist asymmetrisch.** `"*"` = alle Integrationen zuweisen, `""` = **alle entfernen**, sonst Komma-liste von Kanal-UUIDs oder exakten Kanalnamen. Kanäle ohne Namen sind nur per UUID adressierbar. Kanäle auflisten: `GET /api/v3/channels/`.
5. **POST ist nicht idempotent** — ohne `unique` entsteht bei jedem Lauf ein zweiter Check (Free-Konto-Limit 20 → dann **403**). Idempotenz: `"unique": ["slug"]` (akzeptiert `name`, `slug`, `tags`, `timeout`, `grace`): kein Treffer → 201 neu, Treffer → 200 aktualisiert.

## 4. Rezepte (alle über das CLI, Key nie sichtbar)

```bash
KEY=$(cat ~/.healthchecks-credentials)
API=https://healthchecks.io/api/v3
H="X-Api-Key: ${KEY}"

# (a) Inventar lesen — nur unkritische Felder ausgeben
curl -fsS -m 20 --header "$H" "$API/checks/" \
  | python3 -c 'import json,sys
for c in sorted(json.load(sys.stdin)["checks"], key=lambda x: x.get("slug") or x["name"]):
    print(json.dumps({k: c.get(k) for k in ("name","slug","tags","schedule","tz","timeout","grace","status")}))'

# (b) Einzelnen Check nach slug finden (liefert uuid + ping_url -> nicht ausgeben!)
curl -fsS -m 20 --header "$H" "$API/checks/?slug=<slug>"

# (c) Check anlegen (idempotent), Schedule + Zeitzone + Grace in Sekunden
curl -fsS -m 20 --header "$H" -X POST "$API/checks/" \
  --data '{"name":"<Name>","slug":"<slug>","tags":"<tag> <tag>","schedule":"5 * * * *","tz":"Europe/Berlin","grace":7200,"channels":"*","unique":["slug"]}'

# (d) Bestehenden Check ändern (uuid aus (b), im Shell-Kontext halten)
curl -fsS -m 20 --header "$H" -X POST "$API/checks/${U}" \
  --data '{"schedule":"5 * * * *","tz":"Europe/Berlin","grace":7200}'

# (e) Verifikation: Status + letzter/nächster Ping
curl -fsS -m 20 --header "$H" "$API/checks/${U}" \
  | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["status"], c["last_ping"], c["next_ping"], c["schedule"], c["tz"], c["grace"])'
```

Leere POSTs (`pause`/`resume`) brauchen `--data ""`, damit curl einen `Content-Length` sendet: `POST $API/checks/${U}/pause`.

## 5. Verifikationsprotokoll (Pflicht nach jedem Anlegen/Ändern)

Ein Check gilt erst als verifiziert, wenn der Zyklus durchlaufen ist — der HTTP-Code des Pings allein beweist nichts:

1. Erfolgsping senden → `status` = `up`, `last_ping` gesetzt, `next_ping` plausibel zum Schedule.
2. `/fail`-Ping senden → `status` = `down` (beweist: Alarmweg aktiv).
3. Erfolgsping senden → zurück auf `up`.
4. **Vorher ankündigen, dass die zwei Alarm-Mails dieses Test beabsichtigt sind.**
5. Skript-Pings auf fernen Hosts: Ping-Ausfall kann **stumm** sein (fehlendes `curl` → Exit 127, im Skript oft als Warnung schluckt). Verifikation immer über die Management-API, nie über den Exit-Code des Pings.

## 6. Fehlercodes

| Code | Ursache | Erste Maßnahme |
|---|---|---|
| 401 | Key fehlt/falsch, oder `Bearer` statt `X-Api-Key` | Header korrigieren; Key-Datei prüfen (`600`) |
| 403 | Falscher Key **oder** Kontolimit (Free: 20 Checks) **oder** Schreibversuch mit Read-only-Key | Inventar zählen; Key-Typ prüfen |
| 404 | Check existiert nicht — **auch** wenn `/flips/` mit **slug** statt `uuid` aufgerufen wird | `uuid` aus (b) verwenden |
| 409 | Slug nicht eindeutig (Ping-API) / Check nicht im `paused`-Zustand (resume) | Slugs im Projekt eindeutig machen |
| 400 | Invalides Feld (z. B. `grace` < 60, Slug-Zeichen außerhalb `a-z 0-9 - _`) | Feldwerte gegen die Referenz prüfen |
| 429 | Rate Limit: >5 Pings/min je Check, >100 API-Reqs/min | Testläufe drosseln, nicht retryen |

**Antwortform-Falle:** `/flips/` liefert laut Doku eine Liste, real ein Objekt `{"flips": […]}` — JSON-Parsing defensiv schreiben.

## 7. Referenz

Vollständige Endpoint- und Parameter-Tabelle (Management-API v3 + Ping-API): `references/api-v3.md`. Stand der Doku: 2026-10-22, live gegen unser Konto verifiziert. Bei API-Fragen: Doku unter `https://healthchecks.io/docs/api/` (v3) und `https://healthchecks.io/docs/http_api/` (Ping) lesen — nie aus Erinnerung.
