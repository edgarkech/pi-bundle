# Referenz: healthchecks.io API (Stand 2026-10-22)

Quellen: `https://healthchecks.io/docs/api/` (Management API v3), `https://healthchecks.io/docs/http_api/` (Pinging API).
Endpunkte und Felder am 2026-10-22 gegen das eigene Konto live verifiziert (read-only + ein Anlage-/Änderzyklus). Abweichungen zur Doku sind mit **⚠ live** markiert.

---

## A. Management API v3

**Basis:** `https://healthchecks.io/api/v3/` · Auth: Header `X-Api-Key: <key>` (alternativ Feld `api_key` im JSON-Body — aus `ps`-Sicht sichtbar, bei uns verbotene Form).
Keys sind **projektbezogen**, es gibt keine kontoweiten Keys. Ein Projekt hat standardmäßig **keinen** Key; read-write und read-only werden im Project Settings-Blatt angelegt.
POST erwartet ein **JSON-Body** (kein `multipart/form-data`).
Rate Limit: ~100 Requests/min → danach 429.

### Endpunkte

| Zweck | Aufruf |
|---|---|
| Checks auflisten | `GET /checks/` — Filter `?slug=<wert>`, `?tag=<wert>` (wiederholbar) |
| Einzelner Check | `GET /checks/<uuid>` · `GET /checks/<unique_key>` |
| Check anlegen | `POST /checks/` |
| Check ändern | `POST /checks/<uuid>` (nur gesendete Felder ändern sich) |
| Pausieren | `POST /checks/<uuid>/pause` (leerer Body → `--data ""`) |
| Fortsetzen | `POST /checks/<uuid>/resume` (409, wenn nicht `paused`) |
| Löschen | `DELETE /checks/<uuid>` |
| Ping-Historie | `GET /checks/<uuid>/pings/` (Free: 100 Pings, Paid: 1000) |
| Ping-Body | `GET /checks/<uuid>/pings/<n>/body` |
| Statuswechsel | `GET /checks/<uuid>/flips/` — Filter `?seconds=<n>` \| `?start=<unix>` \| `?end=<unix>` · **⚠ live:** Antwort ist `{"flips":[{timestamp,up}]}`, nicht die dokumentierte nackte Liste; **slug** wird hier nicht akzeptiert (404) |
| Integrationen | `GET /channels/` → `id`, `name`, `kind` |
| Badges | `GET /badges/` (svg / json / shields, je mit 3-State-Variante `-3`; Pseudo-Tag `*` = Gesamtstatus) |
| Dienststatus | `GET /status/` (Datenbank-Test, ohne Auth) |

**Read-only-Key:** funktioniert nur auf *Checks auflisten*, *Einzelner Check*, *Statuswechsel*, *Badges*; blendet `uuid`, `ping_url`, `update_url`, `pause_url`, `resume_url`, `channels` aus und liefert stattdessen `unique_key` (stabil über Aufrufe). Skripte, die `uuid`/`ping_url` erwarten, brechen mit einem Read-only-Key.

### Felder eines Checks (Response)

`name` · `slug` · `tags` (leerzeichengetrennt) · `desc` · `timeout` · `grace` · `schedule` · `tz` · `status` (`new` \| `up` \| `grace` \| `down` \| `paused`) · `started` · `last_ping` · `next_ping` · `n_pings` · `manual_resume` · `methods` · `uuid` · `ping_url` · `update_url` / `pause_url` / `resume_url` · `badge_url` · `channels` · Keyword-Filter (`start_kw`, `success_kw`, `failure_kw`, `filter_subject`, `filter_body`, `filter_http_body`, `filter_default_fail`).

### Parameter beim Anlegen/Ändern

| Feld | Typ / Grenzen | Semantik |
|---|---|---|
| `name` | String ≤ 100 | Anzeigename |
| `slug` | String ≤ 100, nur `a-z 0-9 - _` | **v3:** wird nicht mehr aus `name` generiert — explizit setzen, sonst leer |
| `tags` | String, leerzeichengetrennt | Gruppierung, Badge-Zugriff |
| `desc` | String ≤ 10000 | Beschreibung |
| `timeout` | Zahl, **Sekunden**, 60 … 31536000 (Default 86400) | Erwarteter Period; nur wirksam ohne `schedule` |
| `grace` | Zahl, **Sekunden**, 60 … 31536000 (Default 3600) | Nachlauf, in dem ein verspäteter Ping noch gilt |
| `schedule` | String, Cron **oder** systemd `OnCalendar` (Autodetect) | **gewinnt über `timeout`** |
| `tz` | String, Default `UTC` | wirkt **nur** mit `schedule` |
| `manual_resume` | Bool, Default `false` | `false`: Ping beendet `paused` automatisch |
| `methods` | `""` (HEAD/GET/POST erlaubt) oder `"POST"` | `"POST"` härtet die Ping-URL ab |
| `channels` | `"*"` = alle zuweisen · `""` = alle entfernen · UUID-Liste · exakte Namen (Komma, kein Komma im Namen) | asymmetrisch: kein Feld = unverändert |
| `unique` | Liste aus `name`, `slug`, `tags`, `timeout`, `grace` | **Upsert:** Treffer → 200 aktualisiert, kein Treffer → 201 neu |
| Keyword-Filter | `start_kw`/`success_kw`/`failure_kw` (Komma-liste, case-sensitiv) + `filter_subject` / `filter_body` / `filter_http_body` / `filter_default_fail` | Klassifikation eingehender Mail-/HTTP-Pings; `filter_default_fail=true` macht „kein Treffer" zu *fail* |

**Grace-Verständnis:** grace absorbiert Laufzeit-Streuung und Uhrdrift. Echte Job-Fehler alarmieren sofort über `/fail`; Ausbleibe-Alarme nicht zu eng takten. Alarm-Latenz eines toten Hosts ≈ **Schedule-Intervall + grace**.

**Seconds-Spickzettel:** 600 = 10 min · 3600 = 1 h · 7200 = 2 h · 14400 = 4 h · 43200 = 12 h · 604800 = 7 d.

### Antwortcodes (Management)

`201` neu angelegt · `200` ok/gefunden/aktualisiert · `400` malformed/ungültiger Wert · `401` Key fehlt/ungültig · `403` falscher Key, Kontolimit (Free 20 Checks), Schreibrecht fehlt · `404` Check existiert nicht · `409` resume auf nicht-pausiertem Check · `429` Rate Limit.

---

## B. Pinging API

**Basis:** `https://hc-ping.com/…` · **keine Auth** — die UUID ist das Schreib-Token.
Methoden: `HEAD`, `GET`, `POST` (POST-Diagnose-Body wird bis 100 kB gespeichert; Header `Ping-Body-Limit` nennt das Limit).
Rate Limit: nicht öfter als 5 Pings/min je Check.

| Signal | UUID-Form | Slug-Form |
|---|---|---|
| Erfolg | `https://hc-ping.com/<uuid>` | `https://hc-ping.com/<ping-key>/<slug>` |
| Start | `…/<uuid>/start` | `…/<ping-key>/<slug>/start` |
| Fail | `…/<uuid>/fail` | `…/<ping-key>/<slug>/fail` |
| Log (Status unverändert) | `…/<uuid>/log` | `…/<ping-key>/<slug>/log` |
| Exit-Status (0 = Erfolg) | `…/<uuid>/<0-255>` | `…/<ping-key>/<slug>/<0-255>` |

**Query-Parameter:** `rid=<uuid>` (Run-ID, paart start↔Erfolg und misst Laufzeit) · `create=0|1` nur in der Slug-Form (Auto-Provisioning, Default 0).

**Antwortcodes Ping-API:** `200 OK` · **`200 OK (not found)`** — Check unbekannt, Request ignoriert! · `200 OK (rate limited)` · `201 Created` (Slug + `create=1`) · `400 invalid url format` · `404` (nur `/log`) · `409 ambiguous slug` · `429` (Slug-Form).

**Konsequenz:** Ein Ping sagt über seinen Erfolg nichts aus. Verifikation immer über die Management-API (`status`, `last_ping`, `pings/`).

**UUID vs. Slug:** UUID ist immutabel und fällt beim Löschen des Checks weg. Slug ist frei wählbar, änderbar, **nicht garantiert eindeutig** (409 bei Mehrdeutigkeit). Der Projekt-`ping-key` ist ein zusätzliches Schreib-Token — wie die UUID behandeln.

**Altform:** `hc-ping.com/api/v1/<uuid>/fail` funktioniert (in älteren Skripten im Einsatz); neue Skripte nutzen die kurze Form.

---

## C. Skript-Seite (Bewährtes aus dem Betrieb)

```sh
# Erfolgsping, hartes Timeout, kein Exit-Code-Auswertungszwang am Signal selbst
curl -fsS -m 15 "$PING_URL"      >/dev/null 2>&1
# Fail-Ping (Sofortalarm, umgeht die grace-Wartezeit)
curl -fsS -m 15 "$PING_URL/fail" >/dev/null 2>&1
```

- `curl` muss auf dem Zielsystem **installiert** sein. Auf greenfield hat fehlendes curl (Exit 127) den Ping **monatelang stumm** sterben lassen, während das Backup selbst grün lief.
- Update-/Wartungsfenster: während eines laufenden Jobs keine Pings senden (Jail-Neustarts mitten im Lauf sind keine Befunde) — etabliertes Muster: Lock-Datei mit Alterprüfung.
- Ein Alarmkanal halten: ein Skript schreibt `crit.state`, der Healthcheck macht daraus den `/fail`-Ping — oder der Check pingt selbst. Nicht beides parallel.
- FreeBSD-Host mit Login-Shell `csh`: Remote-Kommandos ohne `$(…)`, alternativ `ssh host 'sh -c "…"'`.
