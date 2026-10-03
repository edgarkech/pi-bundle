#!/usr/bin/env python3
"""pib — pi-bundle Memory-Engine (Port Schritt 4a).

Single-File-Port der Memory-Engine in die pi-bundle-Grammatik. Refactor nur
an den Rändern (Konfig-Quelle, CLI-Grammatik, generische Namen); die
Engine-Logik ist 1:1 übernommen (Validierung vor Schreiben, atomare
Operationen, Fehlerklassen, Normalform-Dedup, Teil-Commits).

Kern-Disziplinen (unverändert übernommen):

* §2   Träger/Schema: EINE ``sockel.md`` je Topic (Bereiche als Abschnitte)
       + ``provenienz.md`` + ``snapshots/`` — kein SUMMARY/FACTS/TIMELINE mehr.
* §3   Paketformat je Eintrag (``eintraege[]`` mit intra-Paket-``id``),
       zwei Pakettypen ``topic``/``domain``, zwei Fächer
       (``staging/<topic>/`` · ``staging/_domains/``), Homogenität,
       schmales Operations-Vokabular (§3.4) — inkl. Domänen-Format §3.4a:
       drei Domain-Ops (``UPSERT_DOMAIN`` Entry-Anlage ·
       ``UPSERT_DOMAIN_ITEM`` nummerierte Items, append-only ·
       ``REMOVE_DOMAIN_ITEM``), Meta-Header ``entry-key``, ``create_domain``
       als vierter Einzelakt.
* §5   Provenienz je Eintrag (on-demand) + Snapshots nach jedem Commit
       (Stelle-4-Mechanik); TIMELINE läuft aus.
* §6   Spielart ``austausch`` + Auto-Commit (Schaltgröße = Dispatch-Matrix,
       ``auto_commit``-Sockel-Flag entfernt — E2; A5 für Domänen-Pakete).
* §9   Digest-Bau (B1–B5, B4 integriert ``sockel.md``) — im Port enthalten,
       wird erst in Schritt 4c verdrahtet.

Two-Phase-Commit (CONCEPT 5.3):
    Validieren → Staging (Temp unter der Basis) → atomarer Swap
    (``os.replace``). Ein einziger Validierungsfehler verwirft das ganze
    Commit; der Live-Zustand bleibt **byte-identisch** (P1/P2). Das Echo
    meldet die Fehlerursache wörtlich (P1 — keine stillen Zustände).

Konfiguration:
    Root = Env ``PI_BUNDLE_HOME`` > Default ``~/.pi/pi-bundle``; die
    Memory-Basis (dort weiter unter ``[paths].memory``) ergibt sich aus der
    zentralen ``config.toml``. Fehlt die Konfig oder ist der Root nicht
    auflösbar → lauter Fehler (kein Default-Raten auf Datenpfade).

Kein Schreibzugriff außerhalb der Memory-Basis; die Engine schreibt nie in
den Sessions-Root (nur Lese-Verträge für Digest-/Pipeline-Zähler).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import worker  # Worker-Kern (Schritt 4b): Task-Format, Queue, Validierung, Watchdog


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EngineError(Exception):
    """Agent-lesbarer, strukturierter Fehler mit klarer Ursache (P1).

    Kein ``print``/``sys.exit`` in der Bibliothek — die CLI-Schicht
    übersetzt in stdout/stderr + Exit-Codes (§4.7: 0 ok · 1 Validierung/
    I/O · 2 Argumente).
    """


# ---------------------------------------------------------------------------
# Vertragskonstanten (ENGINE-FASSUNG-SPEC)
# ---------------------------------------------------------------------------

#: Env-Variable für den Bundle-Root (kein Default-Raten auf Datenpfade).
ENV_BUNDLE_HOME = "PI_BUNDLE_HOME"

#: Default-Bundle-Root (überschreibbar via Env/``--home``).
DEFAULT_BUNDLE_HOME = "~/.pi/pi-bundle"

#: Konfig-Dateiname im Bundle-Root (die einzige Konfig-Quelle).
CONFIG_NAME = "config.toml"

#: Pi-Standard-Ort der unverdichteten Sessions (CONCEPT Grundannahme 3).
#: Nur LESEN (build-digest, Pipeline-Zähler) — die Engine schreibt nie
#: dorthin. Test/Dev-Override via Konfig ``[paths].sessions_root`` oder Env.
ENV_SESSIONS_ROOT = "PI_BUNDLE_SESSIONS_ROOT"
DEFAULT_SESSIONS_ROOT = "~/.pi/pi-bundle/sessions"


# ---------------------------------------------------------------------------
# Konfig-Loader (config.toml via tomllib — nur Python 3.11+ Stdlib)
# ---------------------------------------------------------------------------

def resolve_bundle_root(override: Optional[str] = None) -> Path:
    """Bundle-Root auflösen: ``--home`` > Env ``PI_BUNDLE_HOME`` > Default
    ``~/.pi/pi-bundle``. Der Root ist immer auflösbar (Pfad-Existenz wird
    erst beim Konfig-Ladeversuch geprüft)."""
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_BUNDLE_HOME)
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_BUNDLE_HOME).expanduser()


def load_config(root: Path) -> dict:
    """``config.toml`` aus dem Bundle-Root laden (P1: fehlt die Konfig oder
    ist sie unlesbar → lauter ``EngineError``, kein Default-Raten)."""
    cfg_path = Path(root).expanduser() / CONFIG_NAME
    if not cfg_path.is_file():
        raise EngineError(
            f"Konfig nicht gefunden: {cfg_path} — lege eine config.toml "
            f"an (Vorlage: config.example.toml) oder setze PI_BUNDLE_HOME "
            f"auf einen Bundle-Root mit Konfig.")
    try:
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise EngineError(f"Konfig unlesbar (config.toml): {e}")
    if not isinstance(cfg, dict):
        raise EngineError("Konfig ist kein TOML-Objekt (config.toml).")
    return cfg


def memory_root_from_config(root: Path, cfg: dict) -> Path:
    """Memory-Basis aus ``[paths].memory`` (relativ zum Bundle-Root, Default
    ``memory``) bilden — die Engine-Basis, unter der die Pfad-Schemata
    (``topics/<name>/sockel.md`` · ``provenienz.md`` · ``snapshots/`` ·
    ``staging/…`` · ``digest/…``) liegen."""
    paths = cfg.get("paths") or {}
    rel = paths.get("memory", "memory")
    if not isinstance(rel, str) or not rel.strip():
        raise EngineError("[paths].memory muss ein nicht-leerer String sein.")
    if Path(rel).is_absolute():
        raise EngineError(
            "[paths].memory muss relativ zum Bundle-Root sein, war "
            f"absolut: {rel!r}.")
    mem = (Path(root).expanduser() / rel).resolve()
    root_res = Path(root).expanduser().resolve()
    if root_res not in mem.parents:
        raise EngineError(
            "[paths].memory liegt außerhalb des Bundle-Roots — nicht "
            "zulässig (kein Schreibzugriff außerhalb des Roots).")
    return mem


def sessions_root_from_config(cfg: dict) -> Path:
    """Sessions-Root: Env > Konfig ``[paths].sessions_root`` > Default."""
    env = os.environ.get(ENV_SESSIONS_ROOT)
    if env:
        return Path(env).expanduser()
    paths = cfg.get("paths") or {}
    sr = paths.get("sessions_root")
    if sr:
        return Path(str(sr)).expanduser()
    return Path(DEFAULT_SESSIONS_ROOT).expanduser()


#: §2.4 — harte Feldgrenzen je Kategorie (Validator-Grenzen; Überschreitung
#: → Validierungsfehler, laut, kein halber Commit).
SKALAR_MAX = 200        # Zeichen — Skalarfelder (Steckbrief/Journal)
LISTEZEILE_MAX = 160    # Zeichen je List-Zeile (Plan, nächste Schritte, …)
EINTRAG_MAX = 500       # Zeichen — Eintrags-Texte (Wissen)
BESCHREIBUNG_MAX = 800  # Zeichen — Beschreibungs-/Eckpunkt-Felder
LISTE_MAX_ITEMS = 30    # Max-Items je Liste

#: §2.4 — Feld-Vokabular von ``SET_FELD``:
#:   name → (Abschnitt, Typ, Kategorie, erlaubte Spielarten|None)
#: Typ: ``str`` · ``strabs`` (absoluter Pfad) · ``enum`` · ``flag`` ·
#:      ``list``.  Kategorie → Grenze: Skalar/Listenzeile/Eckpunkt/Beschreibung.
FELD_SPEZ: Dict[str, Tuple[str, str, str, Optional[Tuple[str, ...]]]] = {
    "titel":             ("steckbrief", "str",    "Skalar",       None),
    "spielart":          ("steckbrief", "enum",   "Skalar",       None),
    "gesamtstatus":      ("steckbrief", "str",    "Skalar",       None),
    "project":           ("steckbrief", "strabs", "Skalar",       ("projekt", "austausch")),
    "beschreibung":      ("steckbrief", "str",    "Beschreibung", None),
    "ziele":             ("steckbrief", "list",   "Eckpunkt",     None),
    "rahmen":            ("steckbrief", "list",   "Eckpunkt",     None),
    "abgrenzung":        ("steckbrief", "list",   "Eckpunkt",     None),
    "fertig_kriterium":  ("steckbrief", "str",    "Skalar",       None),
    "domaenen":          ("steckbrief", "list",   "Eckpunkt",     None),
    "roadmap":           ("plan",       "list",   "List-Zeile",   ("projekt",)),
    "bearbeitung_stand": ("journal",    "str",    "Beschreibung", None),
    "naechste_schritte": ("journal",    "list",   "List-Zeile",   None),
    "blocker":           ("journal",    "list",   "List-Zeile",   None),
    "nachbar_topics":    ("verweise",   "list",   "List-Zeile",   None),
    # §6.2 — austausch-spezifische Steckbrief-Felder (kein auto_commit-Flag,
    # E2 — die Dispatch-Matrix ist die einzige Auto-Commit-Schaltgröße):
    "raum":              ("steckbrief", "str",    "Skalar",       ("austausch",)),
    "plattform":         ("steckbrief", "str",    "Skalar",       ("austausch",)),
    "zweck":             ("steckbrief", "str",    "Skalar",       ("austausch",)),
    "status":            ("steckbrief", "str",    "Skalar",       ("austausch",)),
    "teilnehmer_bots":   ("steckbrief", "list",   "List-Zeile",   ("austausch",)),
}

#: Kategorie → Zeichen-Grenze je Wert/Zeile.
_LIMIT_PER_KATEGORIE: Dict[str, int] = {
    "Skalar": SKALAR_MAX,
    "List-Zeile": LISTEZEILE_MAX,
    "Eckpunkt": BESCHREIBUNG_MAX,
    "Beschreibung": BESCHREIBUNG_MAX,
}

#: §2.2 — Wissen-Subabschnitte (kanonische IDs + Render-Titel).
WISSEN_SUBABSCHNITTE: Tuple[str, ...] = (
    "entscheidungen", "rahmenbedingungen", "relationen", "dokumente",
)
_WISSEN_TITEL: Dict[str, str] = {
    "entscheidungen": "Entscheidungen",
    "rahmenbedingungen": "Rahmenbedingungen",
    "relationen": "Relationen",
    "dokumente": "Dokumente",
}

#: §2.4 — immutable nach Anlage (SET_FELD wird abgelehnt).
IMMUTABLE_FELDER = frozenset(("titel", "spielart"))

#: §2.2 — Sockel-Abschnitte (kanonische IDs + Render-Titel).
SEKTIONEN: Tuple[str, ...] = ("steckbrief", "plan", "journal", "wissen", "verweise")
_SEKTION_TITEL: Dict[str, str] = {
    "steckbrief": "Steckbrief", "plan": "Plan", "journal": "Journal",
    "wissen": "Wissen", "verweise": "Verweise",
}

#: Liste-Felder im Sockel (``- key:`` + ``  - item``).
LIST_FELDER = frozenset(k for k, s in FELD_SPEZ.items() if s[1] == "list")

#: §2.4 — Spielarten (Enum erweitert um ``austausch``, §6).
SPIELARTEN: Tuple[str, ...] = ("projekt", "dauer_betrieb", "einzel", "austausch")

#: Start-Gesamtstatus je Spielart (Lebenszyklus; ``austausch`` = ``status``).
_START_STATUS: Dict[str, str] = {
    "projekt": "aktiv",
    "dauer_betrieb": "dauerhaft",
    "einzel": "begrenzt",
    "austausch": "aktiv",
}

#: §3.2 — ``paket_typ``-Enum (Homogenitäts-Marker der beiden Läufe;
#: KEINE A/B-Herkunft mehr).
PAKET_TYPEN: Tuple[str, ...] = ("topic", "domain")

#: §3.4/§3.4a — Typ-Konsistenz: erlaubte Operationen je Eintragstyp.
#: Domain-Ops (festgezurrt 2026-09-25): Entry-Anlage, Item anhängen/
#: ersetzen (append-only, Lücken erlaubt, nie neu nummeriert), Item
#: entfernen. Ersetzen die freie ``UPSERT_DOMAIN``-Form (Text-Zeile).
OPS_PER_TYP: Dict[str, Tuple[str, ...]] = {
    "topic": ("SET_FELD", "UPSERT_EINTRAG", "REMOVE_EINTRAG"),
    "domain": ("UPSERT_DOMAIN", "UPSERT_DOMAIN_ITEM", "REMOVE_DOMAIN_ITEM"),
}

#: §3.4a — Domänen-Meta-Header, erste Zeile der Domänen-Datei:
#: ``<!-- entry-key: <schluessel-typ> -->`` (legt fest, was als
#: Entry-Schlüssel gilt — z. B. ``name`` für beziehungen).
DOMAIN_META_RE = re.compile(r"^<!--\s*entry-key:\s*(.+?)\s*-->\s*$")

#: §3.4a — Domain-Entry = ``##``-Überschrift (Key-Wert der Entry-Schlüssel).
DOMAIN_ENTRY_HEADING_RE = re.compile(r"^## (.+?)\s*$")

#: §1 — Paket-Datei-Endung (Gate-Vertrag: das Gate scannt *.proposal.json).
PROPOSAL_SUFFIX = ".proposal.json"

#: Staging-Fächer (§3.1): Topic-Paket je Ziel-Topic · Domänen-Paket im
#: eigenen Fach ``_domains`` (reine Paket-Ablage — kein Kandidaten-Puffer,
#: kein Global-Review). ``_hot`` entfällt vollständig.
STAGING_DIRNAME = "staging"
GLOBAL_FACH_DOMAINS = "_domains"

#: §2.5 — interne Eintrags-IDs (on-demand; trailing HTML-Comment im Sockel).
EINTRAG_ID_RE = re.compile(r"^e(\d{3,})$")

#: §3.3 — ``ziel``-Format für domain-Einträge: ``domains/<name>.md``.
DOMAIN_ZIEL_RE = re.compile(r"^domains/[^/]+\.md$")

#: §2 / §3.2 — ``datum``-Format ``YYYY-MM-DD HH:MM``.
DATUM_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

#: §2.1 — Topic-Namen: kebab-case, ASCII (Pfad-Guard, Code-Kopie).
TOPIC_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# ---------------------------------------------------------------------------
# Digest-Konstanten (§9 — Subcommand ``build-digest``)
# ---------------------------------------------------------------------------

#: B3 — bash-Tokens mit schreibender Wirkung (Wirksamkeits-Heuristik).
WRITE_TOKENS = {">>", "&>", ">", "tee", "cp", "mv", "rm", "rmdir", "unlink",
                "mkdir", "touch", "rsync", "chmod", "chown"}
REDIRECT_RE = re.compile(r"(^|[\s;|&(])>{1,2}[\w./~-]")
SED_I_RE = re.compile(r"\bsed\b[^|;&]*-i\b")
TOKEN_SPLIT_RE = re.compile(r"[\s;|&()]+")

#: B3 — Pfad-Blacklist (vorrangig vor Scope). Keine internen Alt-Pfade.
_BLACKLIST_RAW = [
    r"CHANGELOG\.md", r"/\.pi/", r"/tmp/",
    r"scratch", r"node_modules", r"coverage/", r"/dist/", r"\.git/", r"-backup",
]
BLACKLIST_RE = re.compile("|".join(_BLACKLIST_RAW))

#: B1 — Engine-Verben der Attribution (First-Topic-Signal, pib-Grammatik).
ENGINE_FILENAMES = ("pib.py", "pib")
B1_VERBS = ("topic read", "topic create")

#: B1 — Sekundär-Signal: read-Calls auf den Topic-Stand (Datei-Lese-Pfad,
#: Fix 2026-09-24 — audit/PHASE4-B1-SIGNAL-ERKENNUNG.md).
_SOCKEL_TOPIC_RE = re.compile(r"topics/([A-Za-z0-9_-]+)/sockel\.md")

#: §9.2 — Größenbudget (Warn-Metrik im Kopf, kein Fehlerfall).
DEFAULT_WARN_BUDGET_KIB = 100


# ---------------------------------------------------------------------------
# Normalform (Code-Kopie aus der Alt-Engine)
# ---------------------------------------------------------------------------

def normalform(s: str) -> str:
    """Normalform: NFKC → Trim → Whitespace-Runs → Leerzeichen → lowercase.

    Basis für Dedup (§3.5) und Schlüssel-Matching (Upsert/Remove, §2.5) —
    exakte Gleichheit, kein Fuzzy-Matching.
    """
    s = unicodedata.normalize("NFKC", str(s))
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _is_int(v: Any) -> bool:
    """Strikter int-Check (schließt ``bool`` aus — ``isinstance(True, int)``)."""
    return type(v) is int


def sanitize_sid(sid: str) -> str:
    """§5.2 — Datei-name-Sanitierung der Session-ID: alle Nicht-
    ``[A-Za-z0-9-]`` → ``-`` (Snapshot-Verzeichnisnamen)."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(sid))


# ---------------------------------------------------------------------------
# Kanonische domains/<name>.md-Repräsentation (§3.4a — Pattern-Kopie:
# SockelDoc; zeilenbasiert, Roundtrip-stabil)
# ---------------------------------------------------------------------------

class DomainDoc:
    """Kanonische Domänen-Datei-Repräsentation (§3.4a, festgezurrt
    2026-09-25). Zeilenbasiert — unangetastete Zeilen bleiben byte-
    identisch (kein Format-Drift durch Re-Render fremder Inhalte).

    Struktur:
    * **Meta-Header** in der ersten Zeile: ``<!-- entry-key: <schluessel-\
      typ> -->`` (legen fest, was als Entry-Schlüssel gilt).
    * **Domain-Entry** = ``##``-Überschrift (der Schlüssel-Wert; Match per
      ``normalform`` gegen die Überschriften).
    * **Domain-Item** = nummerierte Zeile ``- <entry>-<n>: <text>``;
      append-only (Lücken erlaubt, **nie neu nummeriert**); die Engine
      vergibt beim Anhängen die nächste freie Nummer (max+1).

    Der INDEX-Spiegel (§2.6) bleibt unberührt: Namen = ``##``-Überschriften
    — der bestehende Verzeichnis-Scan wird nicht angefasst.
    """

    def __init__(self, text: str):
        self.lines: List[str] = text.splitlines()

    @classmethod
    def parse(cls, text: str) -> "DomainDoc":
        return cls(text)

    # ---------- Lesezugriffe -------------------------------------------------

    def meta_key(self) -> Optional[str]:
        """Meta-Header lesen: ``entry-key`` aus der ersten Zeile (fehlt →
        ``None`` — z. B. manuell angelegte Alt-Dateien)."""
        if self.lines:
            m = DOMAIN_META_RE.match(self.lines[0].strip())
            if m:
                return m.group(1)
        return None

    def _sections(self) -> List[Tuple[int, str]]:
        """Alle ``##``-Sektionen als (Zeilenindex, Überschrift)."""
        out: List[Tuple[int, str]] = []
        for i, ln in enumerate(self.lines):
            m = DOMAIN_ENTRY_HEADING_RE.match(ln)
            if m:
                out.append((i, m.group(1)))
        return out

    def find_entry(self, entry: str) -> Optional[Tuple[str, int, int]]:
        """Entry-Sektion per normalform(entry) finden → (Überschrift,
        Startindex, Endindex). Endindex = nächste ``##``-Sektion oder Datei-
        ende (exklusiv). Fehlt → ``None``."""
        want = normalform(entry)
        secs = self._sections()
        for j, (idx, heading) in enumerate(secs):
            if normalform(heading) == want:
                end = secs[j + 1][0] if j + 1 < len(secs) else len(self.lines)
                return heading, idx, end
        return None

    def _item_re(self, heading: str) -> "re.Pattern[str]":
        """Item-Zeilen-Muster einer Sektion: ``- <heading>-<n>: <text>``
        (Heading = tatsächliche Schreibweise in der Datei)."""
        return re.compile(rf"^- {re.escape(heading)}-(\d+): (.*)$")

    def items(self, entry: str) -> Optional[List[Tuple[int, int, str]]]:
        """Items eines Entry als (Zeilenindex, Nummer, Text). Entry fehlt →
        ``None``."""
        found = self.find_entry(entry)
        if found is None:
            return None
        heading, start, end = found
        pat = self._item_re(heading)
        out: List[Tuple[int, int, str]] = []
        for i in range(start + 1, end):
            m = pat.match(self.lines[i])
            if m:
                out.append((i, int(m.group(1)), m.group(2)))
        return out

    def has_item(self, entry: str, item_id: str) -> bool:
        """Item-Existenz per Nummer (``item_id = <entry>-<n>``)."""
        m = re.match(r"^(.*)-(\d+)$", str(item_id))
        if not m:
            return False
        its = self.items(entry)
        return bool(its) and any(n == int(m.group(2)) for _i, n, _t in its)

    def next_item_num(self, entry: str) -> int:
        """Nächste freie Nummer = max(Nummern)+1 (Lücken bleiben, nie neu
        nummeriert); leeres Entry → 1."""
        its = self.items(entry)
        if not its:
            return 1
        return max(n for _i, n, _t in its) + 1

    # ---------- Schreibakte (in-memory; Persistenz = atomarer Swap) ---------

    def upsert_entry(self, entry: str) -> bool:
        """Entry anlegen (``##``-Sektion): fehlt → anhängen (True); existiert
        → no-op (False). Entry = reine Gruppierung, kein Text."""
        if self.find_entry(entry) is not None:
            return False
        if self.lines and self.lines[-1].strip() != "":
            self.lines.append("")
        self.lines.append(f"## {entry.strip()}")
        return True

    def append_item(self, entry: str, text: str) -> str:
        """Item anhängen; Engine vergibt ``<entry>-<n>`` (nächste freie
        Nummer). Entry muss existieren (P1 — Anlage nie still). Rückgabe:
        neues item_id."""
        found = self.find_entry(entry)
        if found is None:
            raise EngineError(
                f"Entry {entry!r} existiert nicht — Entry-Anlage ist ein "
                f"ausdrücklicher Akt (UPSERT_DOMAIN), nie still (§3.4a).")
        heading, _start, end = found
        n = self.next_item_num(entry)
        line = f"- {entry.strip()}-{n}: {text}"
        # Vor einem Sektions-Abschluss-Blank/der nächsten Sektion einfügen.
        pos = end
        if pos > 0 and self.lines[pos - 1].strip() == "":
            pos -= 1
        self.lines.insert(pos, line)
        return f"{entry.strip()}-{n}"

    def replace_item(self, entry: str, item_id: str, text: str) -> None:
        """Item per ``item_id`` ersetzen. Fehlt → Fehler (kein stilles
        Anhängen, §3.4a)."""
        its = self.items(entry)
        if its is None:
            raise EngineError(
                f"Entry {entry!r} existiert nicht — Entry-Anlage ist ein "
                f"ausdrücklicher Akt (UPSERT_DOMAIN), nie still (§3.4a).")
        m = re.match(r"^(.*)-(\d+)$", str(item_id))
        if not m:
            raise EngineError(
                f"item_id {item_id!r} hat nicht das Format '<entry>-<n>'.")
        want = int(m.group(2))
        heading = self.find_entry(entry)[0]  # Heading-Schreibweise der Datei
        for i, n, _t in its:
            if n == want:
                self.lines[i] = f"- {heading}-{n}: {text}"
                return
        raise EngineError(
            f"Item '{item_id}' fehlt in Entry {entry!r} — Ersetzung mit "
            f"fehlendem Item ist ein Fehler (kein stilles Anhängen, §3.4a).")

    def remove_item(self, entry: str, item_id: str) -> bool:
        """Item entfernen (Lücke bleibt, **nie neu nummeriert**).
        Idempotent: fehlend → no-op (False). Entry fehlt → Fehler (P1)."""
        its = self.items(entry)
        if its is None:
            raise EngineError(
                f"Entry {entry!r} existiert nicht — Entry-Anlage ist ein "
                f"ausdrücklicher Akt (UPSERT_DOMAIN), nie still (§3.4a).")
        m = re.match(r"^(.*)-(\d+)$", str(item_id))
        if not m:
            raise EngineError(
                f"item_id {item_id!r} hat nicht das Format '<entry>-<n>'.")
        want = int(m.group(2))
        for i, n, _t in its:
            if n == want:
                del self.lines[i]
                return True
        return False

    def render(self) -> str:
        """Render: Zeilen + abschließender Newline (Datei-Ende kanonisch).
        Unangetastete Zeilen byte-identisch."""
        if not self.lines:
            return ""
        return "\n".join(self.lines) + "\n"



# ---------------------------------------------------------------------------
# Kanonische sockel.md-Repräsentation (Pattern-Kopie: SummaryDoc/FactsDoc)
# ---------------------------------------------------------------------------

@dataclass
class SockelDoc:
    """Single source of truth für ``sockel.md`` (§2.3).

    Kanonischer Parser/Serializer: beide Schreibakte (``create_topic``,
    ``commit_package``) laufen zwingend über diese Klasse — die
    Format-Drift-Klasse wird strukturell eliminiert (Alt-Pattern).

    * ``fields``: Skalar-/Liste-Felder je Abschnitt (Flach-Dict; Ziel-
      Abschnitt steht in ``FELD_SPEZ``).
    * ``eintraege``: Wissen-Einträge je Subabschnitt als
      ``{"schluessel", "text", "id"?}`` — die interne ID (§2.5) lebt nur
      im trailing HTML-Comment und in ``provenienz.md``.
    """

    name: str
    fields: Dict[str, Any] = field(default_factory=dict)
    eintraege: Dict[str, List[dict]] = field(
        default_factory=lambda: {s: [] for s in WISSEN_SUBABSCHNITTE})

    # ---------- Parsing ----------------------------------------------------

    @classmethod
    def parse(cls, text: str, name: Optional[str] = None) -> "SockelDoc":
        doc = cls(name=name or "")
        section: Optional[str] = None
        sub: Optional[str] = None
        current_list: Optional[str] = None
        _sek_inv = {v: k for k, v in _SEKTION_TITEL.items()}
        _sub_inv = {v: k for k, v in _WISSEN_TITEL.items()}

        for line in text.splitlines():
            if line.startswith("# "):
                header = line[2:].strip()
                if " — " in header:
                    doc.name = header.split(" — ", 1)[1].strip()
                continue
            if line.startswith("## "):
                t = line[3:].strip()
                section = _sek_inv.get(t)
                sub = None
                current_list = None
                continue
            if line.startswith("### "):
                t = line[4:].strip()
                sub = _sub_inv.get(t)
                current_list = None
                continue

            # Wissen-Eintrag: ``- <schluessel>: <text> <!-- id:eNNNN -->``
            if section == "wissen" and sub is not None:
                m = re.match(r"^- (.+?): (.*?)\s*(?:<!--\s*id:(e\d+)\s*-->)?\s*$",
                             line)
                if m:
                    doc.eintraege.setdefault(sub, []).append({
                        "schluessel": m.group(1).strip(),
                        "text": m.group(2).strip(),
                        "id": m.group(3),
                    })
                    continue

            # Skalar-/Liste-Feld: ``- <key>: <wert>`` / ``- <key>:``
            if section is not None and sub is None:
                m = re.match(r"^- ([a-z_][a-z0-9_]*): ?(.*)$", line)
                if m:
                    key, rest = m.group(1), m.group(2).strip()
                    if key in LIST_FELDER:
                        doc.fields[key] = []
                        current_list = key
                    else:
                        doc.fields[key] = rest
                        current_list = None
                    continue
                m = re.match(r"^  - (.*)$", line)
                if m and current_list is not None:
                    item = m.group(1).strip()
                    if item:
                        doc.fields[current_list].append(item)
                    continue

            current_list = None
        return doc

    # ---------- Zugriff / Mutation ------------------------------------------

    def get(self, key: str, default: Any = "") -> Any:
        return self.fields.get(key, default)

    def set_field(self, key: str, value: Any) -> None:
        if key not in FELD_SPEZ:
            raise EngineError(f"Unbekanntes Sockel-Feld: {key!r}")
        self.fields[key] = value

    def find_eintrag(self, sub: str, schluessel: str,
                     eintrags_id: Optional[str] = None) -> Optional[dict]:
        """§2.5 — Matching-Ordnung: erst per interne ID (falls angegeben),
        sonst per ``(Subabschnitt, normalform(schluessel))``."""
        entries = self.eintraege.get(sub, [])
        if eintrags_id:
            for e in entries:
                if e.get("id") == eintrags_id:
                    return e
        nk = normalform(schluessel)
        for e in entries:
            if normalform(e["schluessel"]) == nk:
                return e
        return None

    def upsert_eintrag(self, sub: str, schluessel: str, text: str,
                       eintrags_id: Optional[str] = None) -> Tuple[dict, bool]:
        """Upsert je Subabschnitt: Match → ersetzen (ID bleibt stabil),
        sonst anhängen. Rückgabe: (Eintrag, ersetzt?)."""
        entries = self.eintraege.setdefault(sub, [])
        ex = self.find_eintrag(sub, schluessel, eintrags_id)
        if ex is not None:
            ex["schluessel"] = schluessel
            ex["text"] = text
            return ex, True
        neu = {"schluessel": schluessel, "text": text}
        if eintrags_id:
            neu["id"] = eintrags_id
        entries.append(neu)
        return neu, False

    def remove_eintrag(self, sub: str, schluessel: str,
                       eintrags_id: Optional[str] = None) -> bool:
        """§3.4 — Obsoletes wird ersetzt, nicht konserviert (Invariante 1).
        Fehlend → False (Idempotenz, no-op)."""
        entries = self.eintraege.get(sub, [])
        ex = self.find_eintrag(sub, schluessel, eintrags_id)
        if ex is None:
            return False
        entries.remove(ex)
        return True

    def next_eid(self) -> str:
        """§2.5 — nächster fortlaufender Eintrags-ID je Topic (``eNNNN``)."""
        mx = 0
        for entries in self.eintraege.values():
            for e in entries:
                m = EINTRAG_ID_RE.match(e.get("id") or "")
                if m:
                    mx = max(mx, int(m.group(1)))
        return f"e{mx + 1:04d}"

    # ---------- Rendering ----------------------------------------------------

    #: Kanonische Feldreihenfolge im Steckbrief (§2.3/§6.2).
    STECKBRIEF_ORDER: Tuple[str, ...] = (
        "titel", "spielart", "status", "raum", "plattform",
        "zweck", "teilnehmer_bots", "gesamtstatus", "project",
        "beschreibung", "ziele", "rahmen", "abgrenzung", "fertig_kriterium",
        "domaenen",
    )

    def render(self) -> str:
        L: List[str] = [f"# Sockel — {self.name}", ""]

        def scalar(key: str) -> None:
            if key in self.fields:
                L.append(f"- {key}: {self.fields[key]}")

        def list_field(key: str) -> None:
            if key in self.fields:
                L.append(f"- {key}:")
                for it in (self.fields.get(key) or []):
                    L.append(f"  - {it}")

        # Steckbrief (nur vorhandene Felder — Platzhalter statt Fehlfeldern)
        L.append("## Steckbrief")
        for key in self.STECKBRIEF_ORDER:
            if key in LIST_FELDER:
                list_field(key)
            else:
                scalar(key)
        L.append("")

        # Plan — nur Spielart projekt (§2.2-Matrix)
        if self.fields.get("spielart") == "projekt":
            L.append("## Plan")
            list_field("roadmap")
            L.append("")

        # Journal
        L.append("## Journal")
        scalar("bearbeitung_stand")
        list_field("naechste_schritte")
        list_field("blocker")
        L.append("")

        # Wissen (4 Subabschnitte, kanonisch)
        L.append("## Wissen")
        for sub in WISSEN_SUBABSCHNITTE:
            L.append(f"### {_WISSEN_TITEL[sub]}")
            for e in self.eintraege.get(sub, []):
                comment = f" <!-- id:{e['id']} -->" if e.get("id") else ""
                L.append(f"- {e['schluessel']}: {e['text']}{comment}")
            L.append("")

        # Verweise
        L.append("## Verweise")
        scalar("project")
        list_field("nachbar_topics")
        return "\n".join(L) + "\n"


#: IDs/Comments aus dem Sockel-Text stripfen (Normal-Read, §2.5: im
#: Normal-Read unsichtbar; Invariante 1).
_ID_COMMENT_RE = re.compile(r"\s*<!--\s*id:e\d+\s*-->")


def strip_ids(text: str) -> str:
    """Trailing ID-Comments entfernen (menschenlesbarer Stand, Invariante 1/3)."""
    return "\n".join(_ID_COMMENT_RE.sub("", ln) for ln in text.splitlines()) \
        .rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Provenienz je Eintrag (§5.1 — on-demand, nie im Normal-Read)
# ---------------------------------------------------------------------------

PROV_HEADER = ("| id | abschnitt | schluessel | datum | session |",
               "|----|-----------|------------|-------|---------|")


def render_provenienz(name: str, rows: Sequence[dict]) -> str:
    """Kanonische ``provenienz.md``-Tabelle (§5.1)."""
    L = [f"# Provenienz — {name}", ""]
    L.extend(PROV_HEADER)
    for r in rows:
        L.append(f"| {r['id']} | {r['abschnitt']} | {r['schluessel']} "
                 f"| {r['datum']} | {r['session']} |")
    return "\n".join(L) + "\n"


def parse_provenienz(text: str) -> List[dict]:
    """``provenienz.md`` parsen → Zeilen (id/abschnitt/schluessel/datum/session)."""
    rows: List[dict] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        if cells[0] in ("id", "") or set(cells[0]) <= {"-"}:
            continue
        rows.append({"id": cells[0], "abschnitt": cells[1],
                     "schluessel": cells[2], "datum": cells[3],
                     "session": cells[4]})
    return rows


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    """Memory-Engine (pi-bundle): je-Eintrag-Akte, Two-Phase-Commit,
    Lese-Vertrag, Digest-Bau (Subcommand-Bibliothek).

    Öffentliche Schreib-Oberfläche sind exakt die gestatteten Einzelakte
    (§4.2–§4.6) — plus ``read_topic`` (Lese-Vertrag), ``validate_package``
    (reine Prüfung, keine Seiteneffekte) und ``build_digest`` (§9).
    """

    def __init__(self, base_path: Path, sessions_root: Optional[Path] = None):
        self.base_path = Path(base_path).expanduser()
        self.topics_path = self.base_path / "topics"
        self.domains_path = self.base_path / "domains"
        self.index_file = self.base_path / "INDEX.md"
        self.staging_root = self.base_path / STAGING_DIRNAME
        self.digest_root = self.base_path / "digest"
        if sessions_root is None:
            sessions_root = os.environ.get(ENV_SESSIONS_ROOT,
                                           DEFAULT_SESSIONS_ROOT)
        self.sessions_root = Path(sessions_root).expanduser()
        self.topics_path.mkdir(parents=True, exist_ok=True)

    # ---------- Basis-Root / Guards (Pfad-Guard der Port-Basis) ------------

    def _validate_topic_name(self, topic_name: str) -> str:
        """Leere, absolute, Traversal- und ungewöhnliche Namen ablehnen
        (Code-Kopie: Pfad-Guard des Alt-Musters)."""
        if not topic_name or not isinstance(topic_name, str):
            raise EngineError(
                "Topic-Name muss ein nicht-leerer String sein.")
        name = topic_name.strip()
        if not name:
            raise EngineError("Topic-Name darf nicht leer sein.")
        if name in (".", ".."):
            raise EngineError(f"Ungültiger Topic-Name: {name!r}")
        if name.startswith("/") or ".." in name.split("/"):
            raise EngineError(
                f"Topic-Name darf keine Path-Traversal enthalten: {name!r}")
        if any(ord(c) < 32 or c == "\x7f" for c in name):
            raise EngineError(
                f"Topic-Name enthält Steuerzeichen: {name!r}")
        return name

    def _get_topic_dir(self, topic_name: str) -> Path:
        """Topic-Verzeichnis auflösen mit Defense-in-depth (Code-Kopie)."""
        name = self._validate_topic_name(topic_name)
        topic_dir = self.topics_path / name
        try:
            resolved = topic_dir.resolve()
            base_resolved = self.topics_path.resolve()
            if base_resolved not in resolved.parents and resolved != base_resolved:
                raise EngineError(
                    f"Topic-Pfad entkommt der Memory-Basis: {topic_name!r}")
        except OSError:
            raise EngineError(
                f"Topic-Pfad nicht auflösbar: {topic_name!r}")
        return topic_dir

    def _ts_now(self) -> str:
        """``YYYY-MM-DD HH:MM`` (datum-Felder, Provenienz)."""
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _ts_stamp(self) -> str:
        """``YYYYMMDD-HHMMSS`` (Snapshot-Verzeichnisse, §5.2)."""
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    # ---------- create_topic (Einzelakt, §4.2) -------------------------------

    def create_topic(self, name: str, titel: str, spielart: str,
                     projekt: Optional[str] = None,
                     beschreibung: Optional[str] = None) -> dict:
        """Topic-Anlage je Spielart — Sockel-Skelett nach der Spielart-Matrix
        (§2.2). Nicht idempotent: Namens-Kollision → Fehler (Pfad-Guard).

        ``austausch`` (§6): abgespeckter Umfang (kein ``plan``), reduzierter
        Steckbrief nach §6.2 — kein ``auto_commit``-Sockel-Flag (E2, die
        Dispatch-Matrix ist die Auto-Commit-Schaltgröße)."""
        name = self._validate_topic_name(name)
        if not TOPIC_NAME_RE.match(name):
            raise EngineError(
                f"Topic-Name muss kebab-case ASCII sein: {name!r}")
        if not titel or not str(titel).strip():
            raise EngineError(
                "titel ist Pflicht und darf nicht leer sein.")
        if spielart not in SPIELARTEN:
            raise EngineError(
                f"spielart muss eines von {list(SPIELARTEN)} sein, war: "
                f"{spielart!r}")
        if projekt is not None:
            if not str(projekt).startswith("/"):
                raise EngineError(
                    f"projekt muss ein absoluter Pfad sein: {projekt!r}")
            if spielart not in ("projekt", "austausch"):
                raise EngineError(
                    "projekt ist nur für Spielart 'projekt'/'austausch' "
                    f"zulässig, war: {spielart!r}.")
        topic_dir = self._get_topic_dir(name)
        if topic_dir.exists():  # Existenz-Check (nicht idempotent)
            raise EngineError(
                f"CREATE_TOPIC: Namens-Kollision — Topic '{name}' existiert "
                f"bereits.")

        now = self._ts_now()
        doc = SockelDoc(name=name)
        doc.fields["titel"] = str(titel).strip()
        doc.fields["spielart"] = spielart
        if spielart == "austausch":
            # §6.2 — abgespeckter Steckbrief (kein auto_commit-Flag, E2)
            doc.fields["status"] = _START_STATUS["austausch"]
            doc.fields["raum"] = ""
            doc.fields["plattform"] = ""
            doc.fields["zweck"] = ""
            doc.fields["teilnehmer_bots"] = []
        else:
            doc.fields["gesamtstatus"] = _START_STATUS[spielart]
        if beschreibung:
            doc.fields["beschreibung"] = str(beschreibung).strip()
        if spielart == "projekt":
            doc.fields["roadmap"] = []  # §2.2-Matrix: nur Spielart projekt
        if projekt is not None:
            doc.fields["project"] = str(projekt)
        # Journal (alle Spielarten; Zustand, kein Verlauf — Invariante 1)
        doc.fields["bearbeitung_stand"] = f"Anlage {now}"
        doc.fields["naechste_schritte"] = []
        doc.fields["blocker"] = []

        topic_dir.mkdir(parents=True, exist_ok=True)
        (topic_dir / "sockel.md").write_text(doc.render(), encoding="utf-8")
        (topic_dir / "provenienz.md").write_text(
            render_provenienz(name, []), encoding="utf-8")
        (topic_dir / "snapshots").mkdir(parents=True, exist_ok=True)

        self._index_sync(
            topic_name=name,
            status=doc.get("gesamtstatus") or doc.get("status"),
            rebuild_domains=True)
        return {
            "status": "success",
            "message": (f"CREATE_TOPIC: Topic '{name}' angelegt "
                        f"(spielart={spielart})."),
            "topic": name,
        }

    # ---------- create_domain (Einzelakt, §3.4a) -----------------------------

    def create_domain(self, name: str, titel: str,
                      entry_key: str) -> dict:
        """Domänen-Anlage — vierter Einzelakt (§3.4a, festgezurrt
        2026-09-25). Legt ``domains/<name>.md`` mit Meta-Header
        (``<!-- entry-key: <schluessel-typ> -->`` als erste Zeile) + Skelett
        an. Nicht idempotent: Namens-Kollision → Fehler (analog
        ``create_topic``). Voraussetzung: vorgelagerte User-Entscheidung —
        der Befehl selbst prüft das nicht (wie ``create_topic``)."""
        name = self._validate_topic_name(name)
        if not TOPIC_NAME_RE.match(name):
            raise EngineError(
                f"Domain-Name muss kebab-case ASCII sein: {name!r}")
        if not titel or not str(titel).strip():
            raise EngineError(
                "titel ist Pflicht und darf nicht leer sein.")
        if not isinstance(entry_key, str) or not entry_key.strip():
            raise EngineError(
                "entry-key ist Pflicht und darf nicht leer sein (legt fest, "
                "was als Entry-Schlüssel gilt, §3.4a).")
        target = self.domains_path / f"{name}.md"
        if target.exists():  # Existenz-Check (nicht idempotent)
            raise EngineError(
                f"CREATE_DOMAIN: Namens-Kollision — Domain '{name}' "
                f"existiert bereits.")

        self.domains_path.mkdir(parents=True, exist_ok=True)
        text = (f"<!-- entry-key: {entry_key.strip()} -->\n"
                f"# {str(titel).strip()}\n")
        target.write_text(text, encoding="utf-8")

        self._index_sync(rebuild_domains=True)
        return {
            "status": "success",
            "message": (f"CREATE_DOMAIN: Domain '{name}' angelegt "
                        f"(entry-key={entry_key.strip()!r})."),
            "domain": name,
        }

    # ---------- Sockel-/Provenienz-Zugriff -----------------------------------

    def _read_sockel(self, topic_name: str) -> SockelDoc:
        path = self._get_topic_dir(topic_name) / "sockel.md"
        if not path.is_file():
            raise EngineError(
                f"Ziel-Topic '{topic_name}' existiert nicht (sockel.md fehlt).")
        return SockelDoc.parse(path.read_text(encoding="utf-8"),
                               name=topic_name)

    def _read_provenienz(self, topic_dir: Path) -> List[dict]:
        p = topic_dir / "provenienz.md"
        if not p.is_file():
            return []
        return parse_provenienz(p.read_text(encoding="utf-8"))

    # ---------- Validierung (§2.4/§3 — vor jedem schreibenden I/O) -----------

    def validate_package(self, data: Any,
                         exclude_file: Optional[str] = None,
                         subset: Optional[List[dict]] = None) -> None:
        """Vollständige Paket-Validierung in zwei Phasen (§4.3 —
        Subset-/Sequenzsemantik, Defekt-Fix 2026-09-25). Wirft
        ``EngineError`` mit wörtlicher Ursache beim ersten Fehler.
        Keine schreibenden Seiteneffekte — nur Lesezugriffe auf den
        Live-Zustand (Ziel-Existenz).

        Phase 1 — **Struktur-Validierung des vollen Pakets**: Kopf,
        Paket-Homogenität, Intra-Paket-IDs, je-Eintrag-Format (Struktur),
        Typ-Konsistenz (§3.4), Normalform-Dedup (§3.5) —
        **ohne zustandsabhängige Abfragen**. Die harten §2.4-Feldgrenzen
        (Werte gegen Eintrags-Inhalt) laufen dagegen NUR je **gewähltem**
        Eintrag (Subset-Pfad, F2 — Festzurrung 2026-10-03): ein
        grenzverletzender, nicht gewählter Eintrag blockiert den
        Teil-Commit nicht.
        Phase 2 — **Zustands-Validierung nur des freigegebenen Subsets**,
        **sequenziell** gegen den sich entwickelnden Zustand (in-memory-
        Simulation auf dem geparsten Ziel-Doc; gilt für Topic- UND
        Domänen-Pakete). ``subset=None`` → alle Einträge (komplettes
        Paket freigegeben).

        ``commit_package`` führt Phase 1 und Phase 2 getrennt aus, mit
        ``subset`` = gewählter Teilmenge — unfreigegebene Einträge
        blockieren den Commit nicht (§4.3).

        ``exclude_file``: Dateiname, der im Staging-Dedup (§3.5) nicht als
        Peer gezählt wird (das geprüfte Paket liegt selbst noch im Fach)."""
        self._validate_structure(data, exclude_file=exclude_file)
        if subset is None:
            subset = data["eintraege"]
        # §2.4 — harte Feldgrenzen NUR je **gewähltem** Eintrag (F2,
        # Festzurrung 2026-10-03); subset=None → alle Einträge.
        for j, e in enumerate(subset):
            self._validate_feldgrenzen_eintrag(e, data, j)
        self._validate_state(data, subset)

    def _validate_structure(self, data: Any,
                            exclude_file: Optional[str] = None) -> None:
        """§4.3 Phase 1 — Struktur-Validierung des **vollen** Pakets ohne
        zustandsabhängige Abfragen (Kopf, Homogenität, IDs, je-Eintrag-
        Format/Feldgrenzen, Typ-Konsistenz, Normalform-Dedup)."""
        if not isinstance(data, dict):
            raise EngineError("Paket ist kein JSON-Objekt.")

        self._validate_header(data)
        typ = data["paket_typ"]
        self._validate_ziel_kopf(data)

        eintraege = data.get("eintraege")
        if not isinstance(eintraege, list):
            raise EngineError(
                "'eintraege' fehlt oder ist keine Liste.")

        # Homogenität (§3.1): alle Einträge tragen den Paket-Typ.
        for i, e in enumerate(eintraege):
            if not isinstance(e, dict):
                raise EngineError(f"Eintrag #{i} ist kein JSON-Objekt.")
            if e.get("typ") != typ:
                raise EngineError(
                    f"Paket-Homogenität verletzt: Eintrag #{i} hat Typ "
                    f"{e.get('typ')!r} im Paket mit paket_typ={typ!r} "
                    f"(§3.1 — kein 'Reiten' fremder Einträge).")

        # Intra-Paket-Adressen: id Pflicht, eindeutig (§3.3).
        ids = [e.get("id") for e in eintraege]
        for i, eid in enumerate(ids):
            if not isinstance(eid, str) or not eid.strip():
                raise EngineError(
                    f"Eintrag #{i}: 'id' (intra-Paket-Adresse) fehlt oder "
                    f"ist leer.")
        dup = sorted({x for x in ids if ids.count(x) > 1})
        if dup:
            raise EngineError(
                f"Intra-Paket-IDs nicht eindeutig: {dup}.")

        for i, e in enumerate(eintraege):
            self._validate_eintrag(e, data, i)

        # Normalform-Dedup gegen offenes Staging desselben Fachs (§3.5).
        self._check_staging_dedup(data, exclude_file=exclude_file)

    def _validate_header(self, data: dict) -> None:
        """§3.2 — Kopf-Pflichtfelder, Enums, turn_coverage."""
        for key in ("session_id", "erzeugt_durch", "datum"):
            v = data.get(key)
            if not isinstance(v, str) or not v.strip():
                raise EngineError(
                    f"Kopf-Feld '{key}' fehlt oder ist leer.")
        if data["paket_typ"] not in PAKET_TYPEN:
            raise EngineError(
                f"'paket_typ' ungültig: {data.get('paket_typ')!r} "
                f"(erlaubt: {list(PAKET_TYPEN)}).")
        dm = data.get("digest_markierungen")
        if not isinstance(dm, list) or \
                not all(isinstance(x, str) for x in dm):
            raise EngineError(
                "'digest_markierungen' muss eine String-Liste sein "
                "(leere Liste zulässig).")
        if not DATUM_RE.match(data["datum"]):
            raise EngineError(
                f"'datum' hat nicht das Format 'YYYY-MM-DD HH:MM': "
                f"{data['datum']!r}.")
        tc = data.get("turn_coverage")
        if not isinstance(tc, dict):
            raise EngineError(
                "'turn_coverage' fehlt oder ist kein Objekt "
                "(Gate-Vertrag: {turns_seen, turns_total}).")
        seen, total = tc.get("turns_seen"), tc.get("turns_total")
        if not _is_int(seen) or not _is_int(total):
            raise EngineError(
                f"turn_coverage: 'turns_seen' und 'turns_total' müssen int "
                f"sein, waren: {seen!r} / {total!r}.")
        if seen < 0 or total < 0:
            raise EngineError(
                f"turn_coverage: Werte müssen >= 0 sein, waren: "
                f"{seen} / {total}.")
        if seen != total:
            raise EngineError(
                f"turn_coverage: turns_seen ({seen}) != turns_total "
                f"({total}) — Vollverarbeitung des Digests nicht attestiert.")

    def _validate_ziel_kopf(self, data: dict) -> None:
        """§3.2 — ``ziel`` im Kopf: topic → Haupt-Topic (Pflicht);
        domain → entfällt im Kopf (Ziel steht je Eintrag)."""
        typ = data["paket_typ"]
        if typ == "topic":
            ziel = data.get("ziel")
            if not isinstance(ziel, str) or not ziel.strip():
                raise EngineError(
                    "Kopf-Feld 'ziel' (Haupt-Topic) fehlt oder ist leer.")
            self._validate_topic_name(ziel)
            if "/" in ziel:
                raise EngineError(
                    f"ziel für topic muss der Topic-Name sein, war: {ziel!r}.")
        elif typ == "domain":
            if data.get("ziel"):
                raise EngineError(
                    "domain-Paket trägt kein Kopf-'ziel' — das Ziel steht "
                    "je Eintrag in 'eintraege[].ziel' (§3.2/§3.3).")

    def _validate_eintrag(self, e: dict, data: dict, i: int) -> None:
        """§3.3/§3.4 — Eintrag-Validierung, **strukturierte Phase**
        (§4.3 Phase 1): Typ-Konsistenz, Payload, je-Eintrag-Format
        (Struktur), Beleg-Pflicht — ohne zustandsabhängige Abfragen (die
        laufen sequenziell in Phase 2, ``_validate_state``). Die harten
        Feldgrenzen §2.4 (Werte gegen Eintrags-Inhalt) laufen NICHT hier
        über das volle Paket, sondern je **gewähltem** Eintrag in der
        Subset-Phase (``_validate_feldgrenzen_eintrag``, F2 — Festzurrung
        2026-10-03)."""
        op = e.get("operation")
        allowed = OPS_PER_TYP[e["typ"]]
        if op not in allowed:
            raise EngineError(
                f"Typ-Konsistenz verletzt: Operation {op!r} ist für Typ "
                f"{e['typ']!r} nicht erlaubt (erlaubt: {list(allowed)}) — "
                f"Eintrag #{i} ({e.get('id')}).")

        # Beleg-Pflicht (§3.3): turns ⊆ 1..turns_total (Halluzinations-Schutz)
        # oder artefakt-Referenz.
        beleg = e.get("beleg")
        if not isinstance(beleg, dict) or not beleg:
            raise EngineError(
                f"Eintrag #{i} ({e.get('id')}): 'beleg' fehlt oder ist leer "
                f"(Beleg-Pflicht).")
        turns_total = data["turn_coverage"]["turns_total"]
        if "turns" in beleg:
            self._validate_beleg_turns(beleg["turns"], turns_total,
                                       f"Eintrag #{i} ({e.get('id')})")
        elif "artefakt" in beleg:
            if not isinstance(beleg["artefakt"], str) or \
                    not beleg["artefakt"].strip():
                raise EngineError(
                    f"Eintrag #{i} ({e.get('id')}): 'beleg.artefakt' muss "
                    f"ein nicht-leerer String sein.")
        else:
            raise EngineError(
                f"Eintrag #{i} ({e.get('id')}): 'beleg' braucht 'turns' "
                f"oder 'artefakt'.")

        if e["typ"] == "domain":
            self._validate_domain_op(e, i)
            return

        # -- topic-Ops: Domain-Vokabular ist hier nicht zulässig (§3.4a) ----
        if "entry" in e:
            raise EngineError(
                f"Eintrag #{i} ({op}): 'entry' ist nur für Domain-Ops "
                f"zulässig (typ=domain, §3.4a) — war auf typ=topic.")
        if "item_id" in e:
            raise EngineError(
                f"Eintrag #{i} ({op}): 'item_id' ist nur für Domain-Item-Ops "
                f"zulässig (typ=domain, §3.4a) — war auf typ=topic.")

        if op == "SET_FELD":
            self._validate_set_feld(e, data, i)
        elif op in ("UPSERT_EINTRAG", "REMOVE_EINTRAG"):
            abschnitt = e.get("abschnitt")
            if abschnitt not in WISSEN_SUBABSCHNITTE:
                raise EngineError(
                    f"Eintrag #{i} ({op}): Abschnitt {abschnitt!r} ist "
                    f"nicht kanonisch (erlaubt: {list(WISSEN_SUBABSCHNITTE)}).")
            schluessel = e.get("schluessel")
            if not isinstance(schluessel, str) or not schluessel.strip():
                raise EngineError(
                    f"Eintrag #{i} ({op}): 'schluessel' fehlt oder ist leer.")
            if op == "UPSERT_EINTRAG":
                text = e.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise EngineError(
                        f"Eintrag #{i} (UPSERT_EINTRAG): 'text' fehlt oder "
                        f"ist leer.")
                # §2.4 — EINTRAG_MAX-Prüfung läuft NICHT hier (Struktur-
                # Phase über das volle Paket), sondern je gewähltem Eintrag
                # in der Subset-Phase (_validate_feldgrenzen_eintrag, F2).

    def _validate_domain_op(self, e: dict, i: int) -> None:
        """§3.4a — Domain-Ops (typ=domain): exakt die drei Ops
        ``UPSERT_DOMAIN`` / ``UPSERT_DOMAIN_ITEM`` / ``REMOVE_DOMAIN_ITEM``
        mit den definierten Payloads — keine Zusatz-Konstrukte.

        Regeln: ``abschnitt``/``schluessel`` nicht zulässig (stattdessen
        ``entry``) · ``text`` nur bei ``UPSERT_DOMAIN_ITEM`` · ``item_id``
        nur bei Item-Ersetzung/-Entfernung (Format ``<entry>-<n>``) ·
        ``ziel``-Format ``domains/<name>.md``.

        Nur die **strukturierte Phase** (§4.3 Phase 1) — die P1-
        Existenz-Pflichten (Zieldatei/Entry/Item) sind zustandsabhängig
        und laufen sequenziell gegen den sich entwickelnden Zustand in
        Phase 2 (``_validate_state``); dort gelten sie paket-intern auch
        für durch vorangehende ``UPSERT_DOMAIN``-Einträge des Subsets
        entstandene Entries (§3.4a/§4.3)."""
        op = e["operation"]
        wo = f"Eintrag #{i} ({e.get('id')}, {op})"

        # §3.4a — stattdessen 'entry': Abschnitt/Schlüssel-Vokabular ist
        # bei Domain-Ops nicht zulässig.
        for forbidden in ("abschnitt", "schluessel"):
            if forbidden in e:
                raise EngineError(
                    f"{wo}: '{forbidden}' ist bei Domain-Ops nicht zulässig "
                    f"(§3.4a — stattdessen 'entry').")

        entry = e.get("entry")
        if not isinstance(entry, str) or not entry.strip():
            raise EngineError(
                f"{wo}: 'entry' fehlt oder ist leer (Entry = '##'-Sektion "
                f"der Domänen-Datei, §3.4a).")

        # 'text' nur bei UPSERT_DOMAIN_ITEM (Entry = reine Gruppierung,
        # kein Text; Remove trägt keinen Text).
        if "text" in e and op != "UPSERT_DOMAIN_ITEM":
            raise EngineError(
                f"{wo}: 'text' ist nur bei UPSERT_DOMAIN_ITEM zulässig "
                f"(§3.4a).")
        if op == "UPSERT_DOMAIN_ITEM":
            text = e.get("text")
            if not isinstance(text, str) or not text.strip():
                raise EngineError(
                    f"{wo}: 'text' fehlt oder ist leer.")

        # 'item_id' nur bei Item-Ersetzung/-Entfernung; Format <entry>-<n>.
        item_id = e.get("item_id")
        if op not in ("UPSERT_DOMAIN_ITEM", "REMOVE_DOMAIN_ITEM"):
            if item_id is not None:
                raise EngineError(
                    f"{wo}: 'item_id' ist nur bei UPSERT_DOMAIN_ITEM/"
                    f"REMOVE_DOMAIN_ITEM zulässig (§3.4a).")
        else:
            if op == "REMOVE_DOMAIN_ITEM" and \
                    (not isinstance(item_id, str) or not item_id.strip()):
                raise EngineError(
                    f"{wo}: 'item_id' ist Pflicht "
                    f"(Format '<entry>-<n>').")
            if item_id is not None:
                m = re.match(rf"^{re.escape(entry.strip())}-(\d+)$",
                             str(item_id))
                if not m:
                    raise EngineError(
                        f"{wo}: 'item_id' muss '<entry>-<n>' sein (Entry "
                        f"{entry!r}), war: {item_id!r}.")

        # Ziel-Format (Struktur; die P1-Existenz-Pflichten für Datei,
        # Entry und Item laufen sequenziell in Phase 2 — _validate_state).
        ziel = e.get("ziel")
        if not isinstance(ziel, str) or not DOMAIN_ZIEL_RE.match(ziel):
            raise EngineError(
                f"{wo}: 'ziel' muss 'domains/<name>.md' sein, war: "
                f"{ziel!r}.")

    def _validate_set_feld(self, e: dict, data: dict, i: int) -> None:
        """§2.4 — SET_FELD-Payload, **strukturierte Phase** (§4.3 Phase 1):
        Vokabular, Abschnitt, Immutable-Felder, Wert-Format (strabs/enum/
        flag) — ohne zustandsabhängige Abfragen. Die **harten Kategorie-
        Grenzen** (``_LIMIT_PER_KATEGORIE``/``LISTE_MAX_ITEMS``) laufen
        NICHT hier über das volle Paket, sondern je gewähltem Eintrag in
        der Subset-Phase (``_validate_set_feld_grenzen``, F2 — Festzurrung
        2026-10-03). Die Spielart-Bindung am Ziel ist zustandsabhängig
        und läuft in Phase 2 (``_check_spielart_binding``) gegen das sich
        entwickelnde Ziel-Doc."""
        feld = e.get("schluessel")  # schluessel = Feld-ID (§3.4)
        if feld not in FELD_SPEZ:
            raise EngineError(
                f"Eintrag #{i} (SET_FELD): Feld {feld!r} ist nicht im "
                f"Vokabular (erlaubt: {sorted(FELD_SPEZ)}).")
        abschnitt = e.get("abschnitt")
        if abschnitt != FELD_SPEZ[feld][0]:
            raise EngineError(
                f"Eintrag #{i} (SET_FELD {feld}): Abschnitt muss "
                f"'{FELD_SPEZ[feld][0]}' sein, war: {abschnitt!r}.")
        if feld in IMMUTABLE_FELDER:
            raise EngineError(
                f"Eintrag #{i} (SET_FELD {feld}): Feld ist immutable nach "
                f"Anlage.")

        _abs, ftyp, kat, _sl = FELD_SPEZ[feld]
        if ftyp == "list":
            wert = e.get("liste")
            if wert is None and isinstance(e.get("text"), str):
                wert = [e["text"]]  # §3.4: „text/Liste“
            if not isinstance(wert, list) or \
                    not all(isinstance(x, str) and x.strip() for x in wert):
                raise EngineError(
                    f"Eintrag #{i} (SET_FELD {feld}): 'liste' muss eine "
                    f"Liste nicht-leerer Strings sein (leere Liste zulässig).")
        else:
            text = e.get("text")
            if not isinstance(text, str) or not text.strip():
                raise EngineError(
                    f"Eintrag #{i} (SET_FELD {feld}): 'text' muss ein "
                    f"nicht-leerer String sein.")
            if ftyp == "strabs" and not text.startswith("/"):
                raise EngineError(
                    f"Eintrag #{i} (SET_FELD {feld}): Wert muss ein "
                    f"absoluter Pfad sein: {text!r}.")
            if ftyp == "enum" and text not in SPIELARTEN:
                raise EngineError(
                    f"Eintrag #{i} (SET_FELD {feld}): Wert muss eine "
                    f"Spielart sein ({list(SPIELARTEN)}), war: {text!r}.")
            if ftyp == "flag" and text.lower() not in ("true", "false"):
                raise EngineError(
                    f"Eintrag #{i} (SET_FELD {feld}): Wert muss 'true' "
                    f"oder 'false' sein, war: {text!r}.")

    def _validate_set_feld_grenzen(self, e: dict, data: dict, i: int) -> None:
        """§2.4 — harte Feldgrenzen eines **gewählten** SET_FELD-Eintrags
        (Werte gegen Eintrags-Inhalt): Kategorie-Grenze
        (``_LIMIT_PER_KATEGORIE``) bzw. ``LISTE_MAX_ITEMS``. Läuft in der
        Subset-Phase, nicht über das volle Paket (F2 — Festzurrung
        2026-10-03): ein grenzverletzender, NICHT gewählter Eintrag
        blockiert den Teil-Commit nicht; ein gewählter grenzverletzender
        Eintrag blockiert weiterhin (laut, atomar). Struktur-/Vokabular-/
        Format-Checks liegen in ``_validate_set_feld``."""
        feld = e.get("schluessel")
        _abs, ftyp, kat, _sl = FELD_SPEZ[feld]
        limit = _LIMIT_PER_KATEGORIE[kat]
        if ftyp == "list":
            wert = e.get("liste")
            if wert is None and isinstance(e.get("text"), str):
                wert = [e["text"]]  # §3.4: „text/Liste“
            if len(wert) > LISTE_MAX_ITEMS:
                raise EngineError(
                    f"Eintrag #{i} (SET_FELD {feld}): {len(wert)} Items > "
                    f"LISTE_MAX_ITEMS ({LISTE_MAX_ITEMS}).")
            for x in wert:
                if len(x) > limit:
                    raise EngineError(
                        f"Eintrag #{i} (SET_FELD {feld}): List-Zeile "
                        f"{len(x)} Zeichen > {kat}-Grenze ({limit}).")
        else:
            text = e.get("text")
            if isinstance(text, str) and len(text) > limit:
                raise EngineError(
                    f"Eintrag #{i} (SET_FELD {feld}): Wert {len(text)} "
                    f"Zeichen > {kat}-Grenze ({limit}).")

    def _validate_feldgrenzen_eintrag(self, e: dict, data: dict,
                                       i: int) -> None:
        """§2.4 — harte Feldgrenzen eines **gewählten** Eintrags (Werte
        gegen Eintrags-Inhalt), unabhängig vom Zustand. Läuft NUR über
        das Freigabe-Subset (F2 — Festzurrung 2026-10-03): ein
        grenzverletzender, NICHT gewählter Eintrag blockiert einen
        Teil-Commit nicht; ein gewählter grenzverletzender Eintrag
        blockiert weiterhin (laut, atomar)."""
        if e["typ"] == "domain":
            # Domain-Fakttexte (``UPSERT_DOMAIN_ITEM``) haben laut Engine
            # KEINE harte Zeichen-Schranke (§3.4a prüft nur nicht-leer) —
            # es gilt eine Curator-Richtlinie (500), keine Engine-Grenze.
            return
        op = e["operation"]
        if op == "UPSERT_EINTRAG":
            text = e.get("text")
            if isinstance(text, str) and len(text) > EINTRAG_MAX:
                raise EngineError(
                    f"Eintrag #{i} (UPSERT_EINTRAG): Eintrags-Text "
                    f"{len(text)} Zeichen > EINTRAG_MAX ({EINTRAG_MAX}).")
        elif op == "SET_FELD":
            self._validate_set_feld_grenzen(e, data, i)

    def _validate_beleg_turns(self, turns: Any, turns_total: int,
                              wo: str) -> None:
        """§3.3 — beleg.turns: nicht leer, alle int, ⊆ 1..turns_total."""
        if not isinstance(turns, list) or not turns:
            raise EngineError(
                f"{wo}: 'beleg.turns' fehlt oder ist leer (Beleg-Pflicht).")
        for t in turns:
            if not _is_int(t):
                raise EngineError(
                    f"{wo}: 'beleg.turns'-Eintrag {t!r} ist kein int.")
            if not (1 <= t <= turns_total):
                raise EngineError(
                    f"{wo}: 'beleg.turns'-Eintrag {t} außerhalb von "
                    f"1..{turns_total}.")

    # ---------- §4.3 Phase 2 — Zustands-Validierung (Subset, sequenziell) --

    def _validate_state(self, data: dict, subset: List[dict]) -> None:
        """§4.3 Phase 2 — **Zustands-Validierung nur des freigegebenen
        Subsets, sequenziell** gegen den sich entwickelnden Zustand:
        Eintrag ``i`` wird gegen den Zustand validiert, der entsteht,
        wenn Einträge ``1..i-1`` des Subsets angewendet wurden (in-memory-
        Simulation auf dem geparsten Ziel-Doc; gilt für Topic- UND
        Domänen-Pakete — z. B. ``UPSERT_DOMAIN`` + ``UPSERT_DOMAIN_ITEM``
        auf denselben Entry in einem Paket).

        Unfreigegebene Einträge blockieren den Commit nicht (§4.3) — ihre
        Gültigkeit wird bei ihrem eigenen Commit geprüft. P1 bleibt
        intakt: Zieldatei fehlt → Fehler · Item-Op ohne Entry (weder live,
        noch durch vorangehenden ``UPSERT_DOMAIN`` im Subset) → Fehler.

        Die Simulation nutzt dieselben Apply-Helfer wie der Commit
        (``_apply_topic_entry``/``_apply_domain_entry``) — Validierung und
        Apply haben damit dieselbe Sicht auf den Zustand."""
        if data["paket_typ"] == "domain":
            docs: Dict[str, DomainDoc] = {}
            for i, e in enumerate(subset):
                wo = f"Eintrag #{i} ({e.get('id')}, {e['operation']})"
                ziel = e["ziel"]
                doc = docs.get(ziel)
                if doc is None:
                    target = self.domains_path / ziel[len("domains/"):]
                    if not target.is_file():
                        raise EngineError(
                            f"{wo}: Zieldatei '{ziel}' existiert nicht — "
                            f"Domänen-Anlage ist ein separater, "
                            f"ausdrücklicher Akt (create_domain, §3.4a).")
                    doc = docs[ziel] = DomainDoc.parse(
                        target.read_text(encoding="utf-8"))
                op = e["operation"]
                entry = e["entry"]
                # P1: Entry muss bei Item-Ops existieren — live ODER durch
                # vorangehenden UPSERT_DOMAIN im Subset entstanden.
                if op in ("UPSERT_DOMAIN_ITEM", "REMOVE_DOMAIN_ITEM"):
                    if doc.find_entry(entry) is None:
                        raise EngineError(
                            f"{wo}: Entry {entry!r} existiert nicht in "
                            f"'{ziel}' — Entry-Anlage ist ein ausdrücklicher "
                            f"Akt (UPSERT_DOMAIN), nie still (§3.4a).")
                    # Ersetzung: fehlendes Item → Fehler (kein stilles
                    # Anhängen).
                    item_id = e.get("item_id")
                    if op == "UPSERT_DOMAIN_ITEM" and item_id is not None \
                            and not doc.has_item(entry, item_id):
                        raise EngineError(
                            f"{wo}: Item '{item_id}' fehlt in Entry "
                            f"{entry!r} von '{ziel}' — Ersetzung mit "
                            f"fehlendem Item ist ein Fehler (kein stilles "
                            f"Anhängen, §3.4a).")
                # Sequenzielle In-memory-Apply (Referenz-Semantik:
                # _commit_domain) — entwickelt den Zustand für i+1 weiter.
                self._apply_domain_entry(doc, e)
        else:
            # P1: Zieldatei fehlt → Fehler (_read_sockel wirft).
            doc = self._read_sockel(data["ziel"])
            for i, e in enumerate(subset):
                if e["operation"] == "SET_FELD":
                    self._check_spielart_binding(e, data, i, doc)
                # Sequenzielle In-memory-Apply (Referenz-Semantik:
                # _commit_topic) — entwickelt den Zustand für i+1 weiter.
                self._apply_topic_entry(doc, e)

    def _check_spielart_binding(self, e: dict, data: dict, i: int,
                                doc: SockelDoc) -> None:
        """§2.4 — Spielart-Bindung am Ziel (Zustands-Check, §4.3 Phase 2):
        läuft gegen das sich entwickelnde Ziel-Doc (roadmap nur projekt;
        austausch-Felder nur austausch). ``spielart`` ist immutable,
        daher stabil über die Sequenz."""
        feld = e.get("schluessel")
        erlaubte = FELD_SPEZ[feld][3]
        if erlaubte is None:
            return
        ziel_spielart = doc.get("spielart")
        if ziel_spielart not in erlaubte:
            raise EngineError(
                f"Eintrag #{i} (SET_FELD {feld}): Feld ist nur für "
                f"Spielart {list(erlaubte)} zulässig, Ziel-Topic "
                f"'{data['ziel']}' hat Spielart {ziel_spielart!r}.")

    # ---------- Staging-Struktur (§3.1 — zwei Pakettypen, zwei Fächer) ------

    def _staging_folder_for(self, data: dict) -> Path:
        """Fach-Verzeichnis je Paket-Typ (§3.1): Topic-Paket →
        ``staging/<topic>/`` · Domänen-Paket → ``staging/_domains/``."""
        if data["paket_typ"] == "topic":
            return self.staging_root / data["ziel"]
        return self.staging_root / GLOBAL_FACH_DOMAINS

    def _open_staging_packages(self, folder: Path) -> List[Path]:
        """Offene Pakete direkt im Fach (ohne done/ und rejected/)."""
        if not folder.exists():
            return []
        return sorted(p for p in folder.glob(f"*{PROPOSAL_SUFFIX}")
                      if p.is_file())

    def _load_package(self, paket_path: Path) -> dict:
        """Paket laden + Gate-Konvention prüfen (Endung .proposal.json)."""
        if not paket_path.name.endswith(PROPOSAL_SUFFIX):
            raise EngineError(
                f"Paket-Dateiname muss auf '{PROPOSAL_SUFFIX}' enden "
                f"(Gate-Vertrag), war: {paket_path.name!r}.")
        if not paket_path.is_file():
            raise EngineError(
                f"Paket-Datei nicht gefunden: {paket_path}")
        try:
            data = json.loads(paket_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise EngineError(f"Paket unlesbar: {paket_path.name} ({e})")
        if not isinstance(data, dict):
            raise EngineError(
                f"Paket ist kein JSON-Objekt: {paket_path.name}")
        return data

    # ---------- Normalform-Dedup gegen offenes Staging (§3.5) ----------------

    @staticmethod
    def _dedup_key(e: dict, kopf_ziel: str) -> Optional[Tuple]:
        """Dedup-Schlüssel je Eintrag: (Zieltyp, Ziel, Abschnitt/Sektion,
        Schlüssel/Item-ID, Normalform-Text). Nur Upsert-Operationen sind
        Dup-Fälle (REMOVE → kein Dup; §3.5)."""
        op = e.get("operation")
        if op == "UPSERT_EINTRAG":
            return ("eintrag", e.get("typ"), e.get("ziel") or kopf_ziel,
                    e.get("abschnitt"), normalform(e.get("schluessel", "")),
                    normalform(e.get("text", "")))
        if op == "UPSERT_DOMAIN":  # §3.4a — Entry-Anlage (Gruppierung)
            return ("domain", e.get("ziel"),
                    normalform(e.get("entry", "")), "__entry__", "")
        if op == "UPSERT_DOMAIN_ITEM":  # §3.4a — Item anhängen/ersetzen
            item = str(e.get("item_id") or "__append__")
            return ("domain", e.get("ziel"),
                    normalform(e.get("entry", "")), item,
                    normalform(e.get("text", "")))
        return None

    def _check_staging_dedup(self, data: dict,
                             exclude_file: Optional[str] = None) -> None:
        """§3.5 — exakte Eintrags-Duplikate (Zieltyp/Ziel/Abschnitt/
        Schlüssel + Normalform-Text) gegen bereits offenes Staging desselben
        Fachs → abgelehnt (Sicherheitsnetz)."""
        kopf_ziel = data.get("ziel") or ""
        own: List[Tuple[int, Tuple]] = []
        for i, e in enumerate(data["eintraege"]):
            k = self._dedup_key(e, kopf_ziel)
            if k is not None:
                own.append((i, k))
        if not own:
            return
        folder = self._staging_folder_for(data)
        for peer in self._open_staging_packages(folder):
            if exclude_file is not None and peer.name == exclude_file:
                continue  # das Paket selbst zählt nicht als Peer
            try:
                peer_data = json.loads(peer.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # kaputtes fremdes Papier blockiert nicht
            if not isinstance(peer_data, dict):
                continue
            pziel = peer_data.get("ziel") or ""
            for pe in (peer_data.get("eintraege") or []):
                if not isinstance(pe, dict):
                    continue
                pk = self._dedup_key(pe, pziel)
                if pk is None:
                    continue
                for i, ok in own:
                    if ok == pk:
                        raise EngineError(
                            f"Normalform-Dedup: Eintrag {data['eintraege'][i].get('id')!r} "
                            f"ist ein exaktes Duplikat gegen offenes Staging "
                            f"({peer.name}) — abgelehnt (§3.5).")

    # ---------- commit_package (Einzelakt, §4.3) -----------------------------

    def commit_package(self, paket_path: Path,
                       eintraege: Optional[Sequence[str]] = None,
                       auto: Optional[bool] = None) -> dict:
        """Commit eines Staging-Pakets — Two-Phase, je Eintrag.

        Ohne ``eintraege``: ganzes Paket (Kurzform). Mit ``eintraege``:
        nur die freigegebene Teilmenge — **Teil-Commit = Normalfall**
        (CONCEPT 3.1). Ablauf: Validierung (§2.4/§3.4) → atomarer Swap →
        Folgeregeln (§5): Provenienz je angewandtem Eintrag · Snapshot ·
        INDEX-Spiegel-Sync. TIMELINE-Zeile entfällt (§5.3).

        ``auto`` (optional): erzwingt die Auto-Provenienz-Labelung. Default
        ``None`` → **keine interne Ableitung**; der Commit läuft Session-
        Provenienz (HITL). Die Pipeline setzt ``auto=True``/``False``
        ausschließlich aus der Dispatch-Matrix (R4/§6.5) — die Engine ist
        matrix-blind und wertet nur den expliziten Parameter aus (E2).

        Validierung (Two-Phase, §4.3 — Subset-/Sequenzsemantik):
        Phase 1 — Struktur-Validierung des **vollen Pakets** ohne
        zustandsabhängige Abfragen; Phase 2 — Zustands-Validierung **nur
        des freigegebenen Subsets, sequenziell** gegen den sich
        entwickelnden Zustand (in-memory-Simulation). Unfreigegebene
        Einträge blockieren den Commit nicht.

        Paket-Buchhaltung: angewandte Einträge werden aus der Paket-Datei
        entfernt (atomar); leer → ``done/``; sonst bleibt es adressierbar."""
        paket_path = Path(paket_path).expanduser()
        data = self._load_package(paket_path)
        # Phase 1 — Struktur-Validierung des vollen Pakets, kein I/O.
        self._validate_structure(data, exclude_file=paket_path.name)

        # §2.4 — harte Feldgrenzen NUR je **gewähltem** Eintrag (F2,
        # Festzurrung 2026-10-03): ein grenzverletzender, NICHT gewählter
        # Eintrag blockiert den Teil-Commit nicht; ein gewählter
        # grenzverletzender Eintrag blockiert weiterhin (laut, atomar).
        # Ohne ``eintraege`` gilt subset = alle Einträge → Grenzprüfung
        # über alle — Verhalten identisch zu heute.
        subset = self._select_subset(data, eintraege)
        for j, e in enumerate(subset):
            self._validate_feldgrenzen_eintrag(e, data, j)
        # Phase 2 — Zustands-Validierung nur des freigegebenen Subsets,
        # sequenziell gegen den sich entwickelnden Zustand (kein I/O).
        self._validate_state(data, subset)
        now = self._ts_now()
        # E2: keine interne Auto-Derivation — nur der explizit übergebene
        # Parameter zählt. ``None`` → Session-Provenienz (HITL).
        session_label = "auto" if auto else data["session_id"]

        typ = data["paket_typ"]
        result: Dict[str, Any] = {"status": "success",
                                  "paket": paket_path.name,
                                  "typ": typ,
                                  "ziel": data.get("ziel", ""),
                                  "auto_commit": auto}
        if typ == "topic":
            result.update(self._commit_topic(data, subset, now,
                                             session_label))
        else:
            result.update(self._commit_domain(data, subset))

        moved = self._finish_package(paket_path, data,
                                     {e["id"] for e in subset})
        if moved:
            result["moved_to_done"] = moved
        return result

    @staticmethod
    def _select_subset(data: dict,
                       eintraege: Optional[Sequence[str]]) -> List[dict]:
        """Freigegebene Teilmenge wählen (intra-Paket-IDs, §3.3)."""
        if not eintraege:
            return list(data["eintraege"])
        by_id = {e["id"]: e for e in data["eintraege"]}
        subset: List[dict] = []
        for eid in eintraege:
            if eid not in by_id:
                raise EngineError(
                    f"Eintrag {eid!r} nicht im Paket (bekannt: "
                    f"{sorted(by_id)}).")
            subset.append(by_id[eid])
        return subset

    @staticmethod
    def _apply_topic_entry(doc: SockelDoc, e: dict) -> dict:
        """Einzelnen Topic-Eintrag in-memory auf das (geparste) Sockel-Doc
        anwenden. **Referenz-Semantik** für die sequenzielle Validierung
        (§4.3 Phase 2) UND den Commit (``_commit_topic``) — Validierung und
        Apply haben damit dieselbe Sicht auf den Zustand.

        Rückgabe: Info-Dict für den Commit-Pfad (``op``, ``label``; bei
        SET_FELD/UPSERT_EINTRAG die Provenienz-Daten ``prov_id``/
        ``prov_sub``/``prov_key``, bei REMOVE_EINTRAG ``prov_remove`` =
        (Subabschnitt, Schlüssel), falls tatsächlich etwas entfernt wurde).
        """
        op = e["operation"]
        if op == "SET_FELD":
            feld = e["schluessel"]
            wert = e.get("liste")
            if wert is None:
                wert = e["text"]
            doc.set_field(feld, wert)
            return {"op": op, "label": f"SET_FELD({feld})",
                    "prov_id": feld, "prov_sub": FELD_SPEZ[feld][0],
                    "prov_key": feld}
        if op == "UPSERT_EINTRAG":
            sub, key, text = e["abschnitt"], e["schluessel"], e["text"]
            eintrag, ersetzt = doc.upsert_eintrag(
                sub, key, text, e.get("eintrags_id"))
            if not eintrag.get("id"):
                eintrag["id"] = doc.next_eid()  # §2.5 — ID beim 1. Commit
            return {"op": op,
                    "label": f"UPSERT_EINTRAG({sub}/{key}"
                             f"{', ersetzt' if ersetzt else ', neu'})",
                    "prov_id": eintrag["id"], "prov_sub": sub, "prov_key": key}
        # REMOVE_EINTRAG
        sub, key = e["abschnitt"], e["schluessel"]
        removed = doc.remove_eintrag(sub, key, e.get("eintrags_id"))
        return {"op": op,
                "label": f"REMOVE_EINTRAG({sub}/{key}"
                         f"{', entfernt' if removed else ', no-op'})",
                "prov_remove": (sub, key) if removed else None}

    def _commit_topic(self, data: dict, subset: List[dict], now: str,
                      session_label: str) -> dict:
        """topic-Paket: Einträge auf Temp-Kopie des Topic-Verzeichnisses
        anwenden; Folgeregeln (Provenienz, Snapshot, INDEX), atomarer Swap.
        (Apply-Semantik teilt sich mit der sequenziellen Validierung über
        ``_apply_topic_entry``, §4.3.)"""
        ziel = data["ziel"]
        topic_dir = self._get_topic_dir(ziel)
        doc = self._read_sockel(ziel)
        prov = self._read_provenienz(topic_dir)

        applied: List[str] = []

        def prov_upsert(eid: str, abschnitt: str, schluessel: str) -> None:
            for r in prov:
                if r["id"] == eid:
                    r["abschnitt"] = abschnitt
                    r["schluessel"] = schluessel
                    r["datum"] = now
                    r["session"] = session_label
                    return
            prov.append({"id": eid, "abschnitt": abschnitt,
                         "schluessel": schluessel, "datum": now,
                         "session": session_label})

        for e in subset:
            info = self._apply_topic_entry(doc, e)
            applied.append(info["label"])
            if info["op"] == "REMOVE_EINTRAG":
                if info.get("prov_remove"):
                    sub, key = info["prov_remove"]
                    prov = [r for r in prov
                            if not (r["abschnitt"] == sub and
                                    normalform(r["schluessel"]) ==
                                    normalform(key))]
            else:
                prov_upsert(info["prov_id"], info["prov_sub"],
                            info["prov_key"])

        # -- Staging: Dateien neu rendern, Snapshot anlegen ------------------
        sockel_text = doc.render()
        with tempfile.TemporaryDirectory(prefix=".brain-stage-",
                                         dir=str(self.base_path)) as tmp:
            staged = Path(tmp) / ziel
            shutil.copytree(topic_dir, staged, symlinks=False)
            (staged / "sockel.md").write_text(sockel_text, encoding="utf-8")
            (staged / "provenienz.md").write_text(
                render_provenienz(ziel, prov), encoding="utf-8")

            # §5.2 — Snapshot nach JEDEM Commit: vollständiger Stand.
            snap_name = self._unique_snapshot_name(topic_dir, data["session_id"])
            snap_dir = staged / "snapshots" / snap_name
            snap_dir.mkdir(parents=True, exist_ok=True)
            (snap_dir / "sockel.md").write_text(sockel_text, encoding="utf-8")

            # -- atomarer Swap (Code-Kopie: Alt-Muster) ----------------------
            for f in sorted(staged.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(staged)
                    dest = topic_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(f, dest)

        # -- Folge: INDEX-Spiegel-Sync (in derselben Änderung, atomar) -------
        self._index_sync(
            topic_name=ziel,
            status=doc.get("gesamtstatus") or doc.get("status") or "",
            rebuild_domains=True)

        return {"applied": applied, "snapshot": f"snapshots/{snap_name}",
                "provenienz_zeilen": len(prov)}

    def _unique_snapshot_name(self, topic_dir: Path, sid: str) -> str:
        """§5.2 — ``<ts>-<sid>`` (sid sanitisiert); Kollision → Suffix."""
        base = f"{self._ts_stamp()}-{sanitize_sid(sid)}"
        cand, n = base, 1
        snap_root = topic_dir / "snapshots"
        while snap_root.exists() and (snap_root / cand).exists():
            n += 1
            cand = f"{base}-{n}"
        return cand

    @staticmethod
    def _apply_domain_entry(doc: DomainDoc, e: dict) -> str:
        """Einzelnen Domain-Eintrag in-memory auf das (geparste) Domänen-
        Doc anwenden. **Referenz-Semantik** für die sequenzielle
        Validierung (§4.3 Phase 2) UND den Commit (``_commit_domain``) —
        Validierung und Apply haben damit dieselbe Sicht auf den Zustand.
        Rückgabe: Applied-Label."""
        op = e["operation"]
        ziel = e["ziel"]
        entry = e["entry"]
        if op == "UPSERT_DOMAIN":
            if doc.upsert_entry(entry):
                return f"UPSERT_DOMAIN(neu: {ziel}: ## {entry})"
            return f"UPSERT_DOMAIN(no-op: {ziel}: ## {entry})"
        if op == "UPSERT_DOMAIN_ITEM":
            item_id = e.get("item_id")
            if item_id is None:
                new_id = doc.append_item(entry, e["text"])
                return f"UPSERT_DOMAIN_ITEM(neu: {ziel}: {new_id})"
            doc.replace_item(entry, item_id, e["text"])
            return f"UPSERT_DOMAIN_ITEM(ersetzt: {ziel}: {item_id})"
        # REMOVE_DOMAIN_ITEM
        if doc.remove_item(entry, e["item_id"]):
            return f"REMOVE_DOMAIN_ITEM(entfernt: {ziel}: {e['item_id']})"
        return f"REMOVE_DOMAIN_ITEM(no-op: {ziel}: {e['item_id']})"

    def _commit_domain(self, data: dict, subset: List[dict]) -> dict:
        """domain-Paket: §3.4a-Domain-Ops je Zieldatei (Ziel je Eintrag):
        ``UPSERT_DOMAIN`` (Entry anhängen; existiert → no-op) ·
        ``UPSERT_DOMAIN_ITEM`` (anmelden mit nächster freier Nummer oder
        Ersetzung per item_id) · ``REMOVE_DOMAIN_ITEM`` (Lücke bleibt,
        nie neu nummeriert; fehlend → no-op). Kein Snapshot (Snapshots sind
        Topic-Mechanik, §2.1/§5.2). Apply-Semantik teilt sich mit der
        sequenziellen Validierung über ``_apply_domain_entry`` (§4.3)."""
        groups: Dict[str, List[dict]] = {}
        for e in subset:
            groups.setdefault(e["ziel"], []).append(e)

        applied: List[str] = []
        for ziel in sorted(groups):
            target = self.domains_path / ziel[len("domains/"):]
            if not target.is_file():  # P1 (Validierung hat bereits geprüft)
                raise EngineError(
                    f"Zieldatei '{ziel}' existiert nicht — Domänen-Anlage "
                    f"ist ein separater, ausdrücklicher Akt (create_domain, "
                    f"§3.4a).")
            original = target.read_text(encoding="utf-8")
            doc = DomainDoc.parse(original)
            for e in groups[ziel]:
                applied.append(self._apply_domain_entry(doc, e))
            out = doc.render()
            if out != original:  # reiner no-op → Datei byte-identisch
                with tempfile.TemporaryDirectory(prefix=".brain-stage-",
                                                 dir=str(self.base_path)) \
                        as tmp:
                    staged = Path(tmp) / target.name
                    staged.write_text(out, encoding="utf-8")
                    os.replace(staged, target)  # atomarer Swap

        # -- Folge: INDEX-Spiegel-Sync — Domänen-Sektionen (§2.6, atomar) ----
        self._index_sync(rebuild_domains=True)
        return {"applied": applied}

    def _finish_package(self, paket_path: Path, data: dict,
                        applied_ids: set) -> Optional[str]:
        """§4.3 — Paket-Buchhaltung: angewandte Einträge entfernen (atomar);
        leer → ``done/``; sonst bleibt das Paket mit den verbleibenden
        (noch adressierbaren) Einträgen im Fach. Kein Verfall."""
        remaining = [e for e in data["eintraege"]
                     if e.get("id") not in applied_ids]
        folder = self._staging_folder_for(data)
        if not remaining:
            done = folder / "done"
            done.mkdir(parents=True, exist_ok=True)
            dest = done / paket_path.name
            if dest.exists():
                dest.unlink()
            shutil.move(str(paket_path), str(dest))
            companion = paket_path.with_suffix(".md")
            if companion.is_file():
                shutil.move(str(companion), str(done / companion.name))
            return str(dest.relative_to(self.base_path))
        new_data = dict(data)
        new_data["eintraege"] = remaining
        tmp = paket_path.with_name(paket_path.name + ".tmpwrite")
        tmp.write_text(json.dumps(new_data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, paket_path)  # atomar
        return None

    # ---------- reject_package (Einzelakt, §4.4) -----------------------------

    def reject_package(self, paket_path: Path,
                       eintraege: Optional[Sequence[str]] = None,
                       grund: Optional[str] = None) -> dict:
        """Paket (oder Teilmenge) verwerfen — **dokumentiert** nach
        ``rejected/`` im Fach des Pakets (kalte Provenienz, kein stiller
        Löschvorgang). Kein Snapshot (Stand ändert sich nicht).

        Bewusst KEINE semantische Validierung: ein defektes Paket muss genau
        deshalb verwerfbar sein. Geprüft wird nur, was für die Ablage
        deterministisch nötig ist: JSON-Objekt + Dateiname-Konvention +
        bekanntes paket_typ (Fach-Routing)."""
        paket_path = Path(paket_path).expanduser()
        data = self._load_package(paket_path)
        typ = data.get("paket_typ")
        if typ not in PAKET_TYPEN:
            raise EngineError(
                f"Paket verwerfbar nicht routbar: paket_typ={typ!r} "
                f"unbekannt (erlaubt: {list(PAKET_TYPEN)}).")
        if typ == "topic" and not data.get("ziel"):
            raise EngineError(
                "Paket verwerfbar nicht routbar: Kopf-'ziel' fehlt.")
        folder = self._staging_folder_for(data)
        rejected_dir = folder / "rejected"
        try:
            if paket_path.parent.resolve() == rejected_dir.resolve():
                raise EngineError(
                    f"Paket liegt bereits in rejected/: {paket_path.name}")
        except OSError:
            pass

        subset = self._select_subset(data, eintraege)
        now = self._ts_now()
        sid = data.get("session_id", "")

        # Dokumentierte Verwerfung: <sid>.<topic>.rejected.json bzw.
        # <sid>.domain.rejected.json (Kopf + Einträge + grund + datum).
        rej_name = (f"{sid}.{data['ziel']}.rejected.json" if typ == "topic"
                    else f"{sid}.domain.rejected.json")
        rejected_dir.mkdir(parents=True, exist_ok=True)
        dest = rejected_dir / rej_name
        if dest.exists():
            try:
                prev = json.loads(dest.read_text(encoding="utf-8"))
                if not isinstance(prev, dict):
                    prev = {}
            except (json.JSONDecodeError, OSError):
                prev = {}
        else:
            prev = {k: data[k] for k in
                    ("session_id", "paket_typ", "erzeugt_durch")
                    if k in data}
            if typ == "topic":
                prev["ziel"] = data.get("ziel")
        prev.setdefault("verworfen_am", now)
        if grund is not None:
            prev["grund"] = grund
        prev["datum"] = now
        merged = list(prev.get("eintraege") or [])
        merged.extend(subset)
        prev["eintraege"] = merged
        tmp = dest.with_name(dest.name + ".tmpwrite")
        tmp.write_text(json.dumps(prev, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, dest)  # atomar

        # Paket-Buchhaltung wie beim Commit (angewandte = verworfene).
        moved = self._finish_package(paket_path, data,
                                     {e["id"] for e in subset})
        out: Dict[str, Any] = {"status": "rejected",
                               "paket": paket_path.name,
                               "typ": typ,
                               "eintraege": [e["id"] for e in subset],
                               "rejected_dokument": str(
                                   dest.relative_to(self.base_path))}
        if grund is not None:
            out["grund"] = grund
        if moved:
            out["moved_to_done"] = moved
        return out

    # ---------- read_topic (Lese-Vertrag, §4.1) ------------------------------

    def read_topic(self, topic_name: str, provenienz: bool = False,
                   snapshot: Optional[str] = None) -> dict:
        """Lese-Vertrag (§4.1): Sockel (menschenlesbar, IDs gestrippt) +
        Staging-Kontext. **Invariante 3** — Provenienz und Snapshot-Liste
        sind hier bewusst NICHT im Normal-Read enthalten; sie werden nur
        on-demand per Flag geladen:

        * ``provenienz=True`` → lädt ``provenienz.md`` on-demand.
        * ``snapshot=<ts>-<sid>`` → lädt den Snapshot statt des Live-Stands
          (Stelle-4-Rückblick, §5.2) inkl. der on-demand Snapshot-Liste.

        Read-only — keine Seiteneffekte."""
        topic_dir = self._get_topic_dir(topic_name)
        sockel_path = topic_dir / "sockel.md"
        if not sockel_path.is_file():
            raise EngineError(
                f"Topic '{topic_name}' existiert nicht (sockel.md fehlt).")
        sockel_raw = sockel_path.read_text(encoding="utf-8")

        result = {
            "name": topic_name,
            "sockel": strip_ids(sockel_raw),
            "index_row": self._index_row_for(topic_name),
            "staging": self._staging_pakete(topic_name),
        }

        if provenienz:
            prov_path = topic_dir / "provenienz.md"
            result["provenienz"] = \
                parse_provenienz(prov_path.read_text(encoding="utf-8")) \
                if prov_path.is_file() else []

        if snapshot is not None:
            if not snapshot or not isinstance(snapshot, str) \
                    or "/" in snapshot or ".." in snapshot.split("/"):
                raise EngineError(
                    f"Ungültiger Snapshot-Name: {snapshot!r} "
                    "(erlaubt: <ts>-<sid>).")
            snap_dir = topic_dir / "snapshots" / snapshot
            snap_sockel = snap_dir / "sockel.md"
            if not snap_sockel.is_file():
                raise EngineError(
                    f"Snapshot '{snapshot}' existiert nicht in Topic "
                    f"'{topic_name}'.")
            result["snapshot"] = snapshot
            result["sockel"] = strip_ids(snap_sockel.read_text(
                encoding="utf-8"))
            # Snapshot-Liste on-demand (Stelle-4-Rückblick, §4.1)
            snap_root = topic_dir / "snapshots"
            result["snapshots"] = sorted(
                p.name for p in snap_root.iterdir()
                if p.is_dir()) if snap_root.is_dir() else []

        return result

    def _staging_pakete(self, topic_name: str) -> List[Dict[str, Any]]:
        """Staging-Block (§4.1): alle OFFENEN topic-Pakete des Topics aus
        ``staging/<topic>/`` (``done/`` / ``rejected/`` sind Unterverzeichnisse
        und werden nicht mitgelesen), je Paket Kopf-Felder + ``eintraege[]``
        **vollständig** (id, typ, operation, abschnitt, schluessel, text,
        beleg) — kein Vorfiltern, keine Kürzung. Leeres Staging → leere Liste
        (explizit). Domänen-Pakete (``staging/_domains/``) gehören NICHT in
        den Topic-Staging-Block (anderes Fach, kein read_topic-Ziel).

        Read-only, keine Seiteneffekte."""
        folder = self.staging_root / topic_name
        pakete: List[Dict[str, Any]] = []
        for p in self._open_staging_packages(folder):
            data = self._load_package(p)
            pakete.append({
                "paket": p.name,
                "kopf": {k: v for k, v in data.items()
                         if k != "eintraege"},
                "eintraege": data.get("eintraege", []),
            })
        return pakete

    def _index_row_for(self, topic_name: str) -> Optional[str]:
        if not self.index_file.is_file():
            return None
        for line in self.index_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"| {topic_name} "):
                return line
        return None

    # ---------- INDEX-Spiegel -------------------------------------------------

    def _index_sync(self, topic_name: Optional[str] = None,
                    status: Optional[str] = None,
                    rebuild_domains: bool = False) -> None:
        """INDEX.md — Spiegel, nie SSoT (CONCEPT 5.1, §2.6):
        Topics-Zeile upserten (scoped auf den Bereich vor der ersten
        ``## DOMAIN``-Sektion) und/oder Domänen-Sektionen (Namen =
        ``##``-Überschriften der Domänen-Datei) per Verzeichnis-Scan
        neu generieren — idempotent, selbstheilend, atomar schreiben."""
        lines: List[str] = []
        if self.index_file.is_file():
            lines = self.index_file.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "# INDEX":
            lines = ["# INDEX", ""]
        # Topics-Tabellen-Header sicherstellen
        if not any(l.startswith("| topic") for l in lines):
            insert_at = 2 if len(lines) >= 2 else len(lines)
            lines[insert_at:insert_at] = ["| topic | status |", "|---|---|"]
        # Erste Domänen-Sektion = Grenze des Topics-Blocks
        dom_start = next(
            (i for i, l in enumerate(lines) if l.startswith("## DOMAIN")),
            None)
        search_end = dom_start if dom_start is not None else len(lines)
        if topic_name is not None:
            new_row = f"| {topic_name} | {status or '—'} |"
            replaced = False
            for i in range(search_end):
                if lines[i].startswith(f"| {topic_name} "):
                    lines[i] = new_row
                    replaced = True
                    break
            if not replaced:
                last_tab = max(
                    (i for i in range(search_end)
                     if lines[i].startswith("|")),
                    default=search_end - 1)
                lines[last_tab + 1:last_tab + 1] = [new_row]
        if rebuild_domains:
            if dom_start is not None:
                lines = lines[:dom_start]
            while lines and lines[-1].strip() == "":
                lines.pop()
            dom_lines: List[str] = []
            for dom_file in sorted(self.domains_path.glob("*.md")):
                # Namen = `##`-Überschriften der Domänen-Datei (§2.6) —
                # Details (Bullet-Zeilen) werden nicht gespiegelt
                names = [l[2:].strip() for l in dom_file.read_text(
                    encoding="utf-8").splitlines()
                    if l.startswith("## ") and l[2:].strip()]
                dom_lines.append("")
                dom_lines.append(
                    f"## DOMAIN {dom_file.stem} - {dom_file.resolve()}")
                dom_lines.extend(f"- {n}" for n in names)
            lines.extend(dom_lines)
        tmp = self.index_file.with_name("INDEX.md.tmpwrite")
        tmp.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        os.replace(tmp, self.index_file)  # atomar

    # ---------- build-digest (§9 — Subcommand) --------------------------------

    # ---------- Pipeline-Zähler (§10.4 — Aufsetzen-Ritual) -------------------

    def pipeline_zaehler(self) -> dict:
        """§10.4 — Pipeline-Rückstände zählen (Aufsetzen-Ritual meldet
        ``queue: n · failed: n · offene Sessions: n``). Rein lesend.

        * ``queue``   = offene Digests in ``digest/queue/``
        * ``failed``  = Digests in ``digest/failed/`` (Eskalations-Zustände)
        * ``offene_sessions`` = noch nicht verdichtete Session-Dateien am
          Pi-Standard-Ort (deren Digest noch nicht in queue/done/failed liegt)
        """
        def _cat(state: str) -> int:
            d = self.digest_root / state
            if not d.is_dir():
                return 0
            return len(list(d.glob("*.digest.md")))

        queue = _cat("queue")
        failed = _cat("failed")
        done_ids: set = set()
        if self.digest_root.is_dir():
            for s in ("queue", "done", "failed"):
                for p in (self.digest_root / s).glob("*.digest.md"):
                    done_ids.add(p.name.replace(".digest.md", ""))
        offene = 0
        if self.sessions_root.is_dir():
            for p in self.sessions_root.rglob("*.jsonl"):
                if p.stem not in done_ids:
                    offene += 1
        return {"queue": queue, "failed": failed,
                "offene_sessions": offene}

    def find_session_file(self, sid: str) -> Path:
        """Session-Datei am Pi-Standard-Ort suchen: ``<sessions_root>/<sid>.jsonl``
        (rekursiv als Fallback)."""
        cand = self.sessions_root / f"{sid}.jsonl"
        if cand.is_file():
            return cand
        hits = sorted(self.sessions_root.rglob(f"{sid}.jsonl")) \
            if self.sessions_root.is_dir() else []
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise EngineError(
                f"SID {sid!r} mehrdeutig ({len(hits)} Kandidaten unter "
                f"{self.sessions_root}).")
        raise EngineError(
            f"Session-Datei für SID {sid!r} nicht gefunden "
            f"(gesucht: {cand}).")

    def build_digest(self, sid: str, session_file: Optional[Path] = None,
                     warn_budget_kib: int = DEFAULT_WARN_BUDGET_KIB) -> Path:
        """Deterministischer Digest-Bau (kein Modell, P2): B1–B5-Reduktion +
        Head-Chain-Auflösung + Parse-Toleranz + Statistik-Zeile +
        Größenbudget-Warnung. Ablage ``digest/queue/<sid>.digest.md``
        (§9.3 — Namensdisziplin D-0003, kein Versionssuffix).

        Read-only gegenüber der Session; schreibt nur den Digest selbst.
        Existierendes Ziel → Fehler (Idempotenz: je Session ein Digest)."""
        if session_file is None:
            session_file = self.find_session_file(sid)
        session_file = Path(session_file).expanduser()
        if not session_file.is_file():
            raise EngineError(
                f"Session-Datei nicht gefunden: {session_file}")

        text = render_digest(self, sid, session_file.read_text(
            encoding="utf-8"), warn_budget_kib=warn_budget_kib)

        queue_dir = self.digest_root / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        dest = queue_dir / f"{sid}.digest.md"
        if dest.exists():
            raise EngineError(
                f"Digest existiert bereits: {dest} (Idempotenz — je "
                f"Session ein Digest).")
        tmp = dest.with_name(dest.name + ".tmpbuild")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)  # atomar
        return dest


# ---------------------------------------------------------------------------
# Digest-Hilfsfunktionen (module-level, testbar; B1–B5, §9.2)


# ---------------------------------------------------------------------------
# Verdrahtung (Schritt 4c): Digest run/sweep, Dispatch, Sweep-Unterbau
# ---------------------------------------------------------------------------
# Port der Verdrahtungs-Orchestrierung (Pipeline-Wrapper + Nacht-Sweep) auf
# die pib-Grammatik. Kern-Logik 1:1, Refactor nur an den Raendern:
#   * Dispatch aus der zentralen config.toml ([dispatch], [[dispatch]]-Zeilen)
#     statt einer separaten CSV-Matrix.
#   * Lauf-Profile aus `lib/profiles/` (repo-lokal, pib-Grammatik).
#   * Enqueue ueber die pib-Worker-Mechanik (worker.create_task, Rolle curator)
#     -- die Tasks landen NUR in der Queue (kein pi-Spawn in diesem Modul).
#   * Auto-Commit ueber den `pib package commit`-Codepfad (Engine.commit_package
#     mit auto=True) -- Provenienz "auto", nie still (F8).
#
# Idempotenz (F4): Doppelverdichtung ausgeschlossen -- das digest/-Fach
# (queue -> done/failed) vermerkt verarbeitete Sessions; ein aenderungsloser
# Lauf (noop) ist ein vollwertiges Ergebnis.

#: profil-Name -> Lauf-Profil-Datei unter lib/profiles/ (pib-Grammatik).
DISPATCH_PROFILES: Dict[str, str] = {
    "topic": "lauf-topic.md",
    "domain": "lauf-domain.md",
    "allgemein": "lauf-allgemein.md",
    "group": "lauf-group.md",
}

#: Queue-Zustaende des Worker-Kerns (Zustand = Ordner, nicht JSON-Feld).
DIGEST_QUEUE_STATES: Tuple[str, ...] = ("pending", "running", "completed",
                                        "failed")

#: Max. Retry-Versuche je Digest -- konfigurierbar via [trigger].max_task_attempts.
MAX_TASK_ATTEMPTS_DEFAULT = 2

_ERR_CAP = 500          # Ursachen woertlich, gedeckelt je Lauf
_FIRST_TOPIC_RE = re.compile(r"^-\s*first_topic:\s*(.+)$", re.M)


class DigestError(EngineError):
    """Digest-Verdrahtung: erwarteter, laut gemeldeter Fehler (P1)."""


class TaskEnqueueError(EngineError):
    """curator-Task-Anlage (teilweise) fehlgeschlagen -- Sweep holt nach."""


class DispatchError(EngineError):
    """Dispatch-Konfiguration fehlerhaft ([dispatch] in config.toml)."""


def _profile_dir() -> Path:
    """Lauf-Profile liegen repo-lokal unter lib/profiles/ (pib-Grammatik)."""
    return (Path(__file__).resolve().parent / "profiles")


def profile_file(profil: str) -> Path:
    """Lauf-Profil-Datei fuer einen profil-Namen (absolut, muss existieren).
    Die Profile bauen die curator-Tasks je Lauf (referenzierte Task-Spec)."""
    name = DISPATCH_PROFILES.get(profil)
    if not name:
        raise DispatchError(f"Unbekanntes Lauf-Profil: {profil!r} "
                            f"(gueltig: {', '.join(DISPATCH_PROFILES)}).")
    return (_profile_dir() / name).resolve()


def load_dispatch_rows(cfg: dict) -> List[dict]:
    """[dispatch] -- Liste von Tabellen; je Zeile ein Verdichtungs-Lauf.

    Felder je Zeile:
      session_typ   Metadatum (Herkunft/Art), nicht dispatch-entscheidend.
      profil        topic | domain | allgemein | group -> lauf-<profil>.md
      auto          ja | nein -- Schaltgroesse fuer den Auto-Commit (F8).
      ziel          optionales Ziel-Topic fuer topic-type Laeufe (sonst aus dem
                    Digest-`first_topic`, Fallback "allgemeine-chats").

    Lauter Fehler bei leeren/ungueltigen Zeilen (kein Default-Raten)."""
    entries = cfg.get("dispatch")
    if not entries:
        raise DispatchError(
            "[dispatch] leer -- die Verdrahtung braucht mindestens eine "
            "[[dispatch]]-Zeile (config.toml).")
    if not isinstance(entries, list):
        raise DispatchError("[dispatch] muss eine [[dispatch]]-Liste sein.")
    rows: List[dict] = []
    for r in entries:
        if not isinstance(r, dict):
            raise DispatchError("[dispatch]: jede Zeile muss eine Tabelle sein.")
        profil = str(r.get("profil") or "").strip()
        if not profil:
            continue
        if profil not in DISPATCH_PROFILES:
            raise DispatchError(
                f"[dispatch].profil={profil!r} unbekannt "
                f"(gueltig: {', '.join(DISPATCH_PROFILES)}).")
        auto = str(r.get("auto") or "nein").strip().lower() \
            in ("ja", "true", "1")
        rows.append({
            "session_typ": str(r.get("session_typ") or "default").strip(),
            "profil": profil,
            "auto": auto,
            "ziel": str(r.get("ziel") or "").strip() or None,
        })
    if not rows:
        raise DispatchError(
            "[dispatch] enthaelt keine Zeile mit gesetztem profil.")
    return rows


def digest_first_topic(digest_path: Path) -> Optional[str]:
    """B1-Attribution aus dem Digest-Kopf (`- first_topic: <x>`). ``None`` =
    kein Arbeit-Topic erkannt (-> Fallback `allgemeine-chats`)."""
    try:
        text = digest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FIRST_TOPIC_RE.search(text)
    if not m:
        return None
    val = m.group(1).strip()
    if val.startswith("(kein ") or val == "(kein Engine-Signal)" or not val:
        return None
    return val


def _resolve_ziel(row: dict, digest_path: Path) -> Optional[str]:
    """Ziel-Topic fuer topic-type Laeufe: explizites ``ziel`` > Digest-`first_topic`
    > Fallback ``allgemeine-chats``. Domain-Laeufe haben kein Ziel-Topic."""
    if row["profil"] == "domain":
        return None
    if row.get("ziel"):
        return row["ziel"]
    return digest_first_topic(digest_path) or "allgemeine-chats"


def _report_path(engine: "Engine", sid: str) -> Path:
    """Session-Report-Pfad (je Lauf als Deliverable in den curator-Task)."""
    return engine.staging_root / "_sessions" / f"{sid}.summary.md"


def _run_one_liner(digest_path: Path, profil: str,
                   ziel_topic: Optional[str] = None) -> str:
    """Task-Einzeiler (<= max_task_chars): traegt Digest-Pfad, Lauf-Typ und --
    bei topic-type Laeufen -- das deterministische Ziel-Topic als einzige
    session-spezifische Werte; alles Uebrige steht im Profil."""
    one = f"Digest {digest_path} verdichten (Lauf {profil}, Profil beachten)."
    if ziel_topic:
        one += f" Ziel-Topic: {ziel_topic}."
    return one


def _write_task_meta(engine: "Engine", sid: str, digest_path: Path,
                     runs: List[dict], laufe: List[dict],
                     attempt: int) -> Path:
    """Verriegelungs-/Fortschritts-Metadaten fuer den Nacht-Sweep -- D2 = A.

    Inkrementell je erfolgreichem Aufruf: 'laufe' listet die bereits erzeugten
    Laeufe (typ + task_id); 'runs' traegt die Dispatch-Zeilen (Profil, Auto-Flag,
    Ziel-Topic). Der Sweep behandelt einen Digest ohne task.json als "Laeufe
    anlegen", mit task.json als "Zustaende pruefen / fehlende Laeufe anlegen"."""
    queue_dir = engine.digest_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "session_id": sid,
        "digest": str(digest_path),
        "report_deliverable": str(_report_path(engine, sid)),
        "runs": runs,
        "laufe": laufe,
        "attempt": attempt,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    meta_path = queue_dir / f"{sid}.task.json"
    tmp = meta_path.with_suffix(".tmpmeta")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, meta_path)
    return meta_path


def _enqueue_curator_runs(engine: "Engine", wc: dict, cfg: dict, sid: str,
                          digest_path: Path, runs: List[dict],
                          existing: Optional[List[dict]] = None,
                          attempt: int = 1) -> List[dict]:
    """Die curator-Tasks je Dispatch-Lauf via worker.create_task (Rolle
    curator). Sequenziell; jeder Task traegt den Session-Report als Deliverable
    und das Gate-Meta. task.json wird nach JEDEM erfolgreichen Aufruf
    inkrementell geschrieben -- stirbt der Lauf mid-way, zeigt die Meta, welche
    Laeufe existieren; der Sweep legt nur die fehlenden nach (Teil-Fortschritt).

    'existing' = bereits erzeugte Laeufe (aus der Meta) -- nur deren fehlenden
    Typs werden angelegt. Rueckgabe: die vollstaendige Lauf-Liste."""
    done = list(existing or [])
    existing_typs = {d.get("typ") for d in done}
    order = {r["profil"]: i for i, r in enumerate(runs)}
    report = _report_path(engine, sid)
    for run in runs:
        if run["profil"] in existing_typs:
            continue
        one = _run_one_liner(digest_path, run["profil"], run.get("ziel"))
        if len(one) > wc["max_task_chars"]:
            raise TaskEnqueueError(
                f"Task-Einzeiler {len(one)} Zeichen "
                f"(Limit {wc['max_task_chars']}).")
        profile = profile_file(run["profil"])
        if not profile.is_file():
            raise TaskEnqueueError(f"Lauf-Profil fehlt (muss vor dem Lauf "
                                   f"existieren): {profile}")
        gate: Dict[str, Any] = {
            "digest_path": str(digest_path),
            "staging_root": str(engine.staging_root),
            "session_id": sid,
        }
        if run.get("ziel"):
            gate["ziel_topic"] = run["ziel"]
        ok, msg, task_id = worker.create_task(
            wc, role="curator", task=one, spec=str(profile),
            deliverables=[str(report)], model=None,
            timeout=wc["default_timeout_sec"],
            extra_fields={"gate": gate})
        if not ok:
            raise TaskEnqueueError(f"curator-Task ({run['profil']}): {msg}")
        done.append({"typ": run["profil"], "task_id": task_id})
        done.sort(key=lambda d: order.get(d.get("typ"), 99))
        _write_task_meta(engine, sid, digest_path, runs, done,
                         attempt=attempt)
        time.sleep(0.05)   # Aufrufer-Disziplin: ID-Kollisions-Schutz
    return done


def digest_process_session(engine: "Engine", wc: dict, cfg: dict,
                           session_file: Path,
                           reason: str = "run") -> Tuple[str, str]:
    """Kernkette je Session (Port der Wrapper-Orchestrierung):

    Filter/Idempotenz -> Digest-Bau (digest/queue/) -> Dispatch aus [dispatch]
    -> curator-Tasks je Dispatch-Zeile (Enqueue in worker/queue/pending, kein
    pi-Spawn) -> inkrementelle task.json-Meta.

    Rueckgabe (status, detail): 'processed' | 'noop'. Fehler (Digest-Bau,
    Enqueue) werden laut als DigestError/TaskEnqueueError gemeldet -- die
    Erhaltungsgarantie des Sweeps holt sie nach."""
    session_file = Path(session_file).expanduser()
    sid = session_file.stem
    digest_root = engine.digest_root
    # Idempotenz: Digest der Session schon bekannt (queue/done/failed) -> noop.
    for state in ("queue", "done", "failed"):
        if (digest_root / state / f"{sid}.digest.md").exists():
            return "noop", f"Digest bereits in digest/{state}/ (Idempotenz)."
    if not session_file.is_file():
        raise DigestError(f"Session-Datei nicht gefunden: {session_file}")

    rows = load_dispatch_rows(cfg)
    digest_path = engine.build_digest(sid, session_file=session_file)
    runs = [{"session_typ": r["session_typ"], "profil": r["profil"],
             "auto": r["auto"], "ziel": _resolve_ziel(r, digest_path)}
            for r in rows]
    laufe = _enqueue_curator_runs(engine, wc, cfg, sid, digest_path, runs,
                                  existing=None, attempt=1)
    return "processed", f"{len(laufe)} Laeufe"


# ---------------------------------------------------------------------------
# Nacht-Sweep (Erhaltungsgarantie + Buchhaltung + Auto-Commit)
# ---------------------------------------------------------------------------

def _digest_known(engine: "Engine", sid: str) -> bool:
    """Ob die Session-ID bereits einen Digest hat (queue/done/failed)."""
    return any((engine.digest_root / s / f"{sid}.digest.md").exists()
               for s in ("queue", "done", "failed"))


def _find_task_state(workspace: Path, task_id: str) -> Optional[str]:
    """Task-Zustand aus dem Queue-Ordner (Konvention: Zustand = Ordner)."""
    for state in DIGEST_QUEUE_STATES:
        if (workspace / "queue" / state / f"{task_id}.json").exists():
            return state
    return None


def _read_task_error(workspace: Path, task_id: str) -> str:
    """Fehlerursache woertlich aus dem Queue-JSON (Watchdog setzt 'error')."""
    for state in DIGEST_QUEUE_STATES:
        p = workspace / "queue" / state / f"{task_id}.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return str(data.get("error") or "(keine Ursache im JSON)")
            except (OSError, json.JSONDecodeError):
                return "(Queue-JSON unlesbar)"
    return "(Task nicht in der Queue gefunden)"


def _sid_of_digest(digest_path: Path) -> str:
    """Session-ID = Digest-Dateiname ohne Suffix '.digest.md'."""
    return digest_path.name.replace(".digest.md", "")


def _move_parts(engine: "Engine", parts: List[Path], state: str,
                stats: dict) -> bool:
    """Raeumung: Teile (Digest, task.json, ...) nach digest/<state>/ (laut, P1)."""
    target_dir = engine.digest_root / state
    target_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for src in parts:
        if not src.exists():
            stats["errors"] += 1
            ok = False
            continue
        dst = target_dir / src.name
        if dst.exists():
            stats["errors"] += 1
            ok = False
            continue
        try:
            os.replace(src, dst)
        except OSError:
            stats["errors"] += 1
            ok = False
    return ok


def _write_error_history(engine: "Engine", sid: str, digest_path: Path,
                         meta: dict, states: dict, wc: dict) -> Path:
    """Fehlerhistorie-Datei <sid>.error.md -- Ursachen woertlich, Eskalation
    statt stiller Wiederholung; Reparatur manuell (P1, keine stillen Zustaende)."""
    failed_dir = engine.digest_root / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    laufe = [d for d in meta.get("laufe", []) if isinstance(d, dict)]
    lines = [
        f"# Fehlerhistorie -- {sid}",
        f"digest: {digest_path}",
        f"versuche: {meta.get('attempt', 1)}",
        "",
        "## Fehlgeschlagene Laeufe",
    ]
    for typ, state in sorted(states.items()):
        if state in ("failed", None):
            lauf = next((d for d in laufe
                         if isinstance(d, dict) and d.get("typ") == typ), {})
            err = _read_task_error(wc["workspace"],
                                   str(lauf.get("task_id", "?")))
            if len(err) > _ERR_CAP:
                err = err[:_ERR_CAP] + "…"
            lines.append(f"- {typ}: task {lauf.get('task_id', '?')} "
                         f"(Queue: {state}) -- {err}")
    lines.append("")
    lines.append("Reparatur: manuell. Danach Digest nach digest/queue/ "
                 "zuruecklegen (Sweep-Regel greift ab Tag 1).")
    path = failed_dir / f"{sid}.error.md"
    tmp = path.with_suffix(".tmperr")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _auto_commit_digest(engine: "Engine", cfg: dict, sid: str,
                        runs: List[dict], stats: dict) -> int:
    """Auto-Commit-AnstoSS im Sweeper-Pfad (F8, Provenienz "auto").

    Die Dispatch-Zeilen mit `auto=ja` autorisieren den Commit ihrer Pakete
    ohne Freigabe; der Sweeper committet NUR die vom Curator erzeugten Pakete
    (keine Bewertung, keine Paket-Erzeugung). Topic-type Laeufe ->
    `staging/<ziel>/<sid>.<ziel>.proposal.json`, Domain-Laeufe ->
    `staging/_domains/<sid>.domain.proposal.json`. Engine bleibt matrix-blind;
    der Codepfad ist `engine.commit_package(paket, auto=True)` (pib package
    commit). Rueckgabe: Anzahl committeter Pakete; -1 bei Engine-Fehler ->
    Eskalation (keine stille Wiederholung/Raeumung)."""
    staging = engine.staging_root
    committed = 0
    for run in runs:
        if not run.get("auto"):
            continue
        if run["profil"] == "domain":
            paket = staging / "_domains" / f"{sid}.domain.proposal.json"
        else:
            ziel = run.get("ziel") or "allgemeine-chats"
            paket = staging / ziel / f"{sid}.{ziel}.proposal.json"
        if not paket.is_file():
            continue
        try:
            res = engine.commit_package(paket, auto=True)
        except EngineError:
            stats["errors"] += 1
            return -1
        if isinstance(res, dict) and res.get("status") == "success":
            committed += 1
    return committed


def reconcile_digest(engine: "Engine", wc: dict, cfg: dict,
                     digest_path: Path, meta_path: Path, stats: dict,
                     dry_run: bool) -> None:
    """Task-Zustands-Pruefung je Digest (D2 = A): alle completed -> Auto-Commit
    (Auto-Flag) + done/ · failed -> Retry bis max · error.md + failed/ ·
    Teil-Fortschritt -> nur die fehlenden Laeufe anlegen · sonst verlaeuft."""
    sid = meta_path.name.replace(".task.json", "")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        stats["errors"] += 1
        return
    laufe = [d for d in meta.get("laufe", []) if isinstance(d, dict)]
    runs = [r for r in meta.get("runs", []) if isinstance(r, dict)]
    effective_runs = runs
    all_typs = {r.get("profil") for r in effective_runs}
    auto_any = any(r.get("auto") for r in effective_runs)
    states = {d.get("typ"): _find_task_state(wc["workspace"],
                                             str(d.get("task_id", "")))
              for d in laufe}
    failed_typs = {t for t, s in states.items() if s in ("failed", None)}
    pending_typs = {t for t, s in states.items()
                    if s in ("pending", "running")}
    missing_typs = all_typs - set(states)

    # 1) Alle Laeufe completed -> Auto-Commit je Auto-Flag, dann Raeumung nach
    #    done/.
    if not failed_typs and not missing_typs and not pending_typs:
        if dry_run:
            if auto_any:
                stats["to_auto"] += 1
            stats["to_done"] += 1
            return
        if auto_any:
            committed = _auto_commit_digest(engine, cfg, sid,
                                            effective_runs, stats)
            if committed < 0:
                stats["escalated"] += 1
                _move_parts(engine, [digest_path, meta_path], "failed", stats)
                return
            stats["auto"] += committed
        if _move_parts(engine, [digest_path, meta_path], "done", stats):
            stats["done"] += 1
        return

    # 2) Eskalation: failed nach max. Versuchen -> error.md + failed/.
    attempt = int(meta.get("attempt", 1))
    trig = cfg.get("trigger") if isinstance(cfg.get("trigger"), dict) else {}
    max_attempts = int(trig.get("max_task_attempts")
                       or MAX_TASK_ATTEMPTS_DEFAULT)
    if failed_typs and attempt >= max_attempts:
        if dry_run:
            stats["to_escalate"] += 1
            return
        _write_error_history(engine, sid, digest_path, meta, states, wc)
        _move_parts(engine, [digest_path, meta_path], "failed", stats)
        stats["escalated"] += 1
        return

    # 3) Nur pending/running und sonst nichts -> verlaeuft.
    if not failed_typs and not missing_typs:
        return

    # 4) Retry (fehlgeschlagene) und/oder fehlende Laeufe anlegen.
    if dry_run:
        if failed_typs:
            stats["to_retry"] += 1
        if missing_typs:
            stats["to_create"] += 1
        return
    keep = [d for d in laufe if d.get("typ") not in failed_typs]
    new_attempt = attempt + (1 if failed_typs else 0)
    try:
        _enqueue_curator_runs(engine, wc, cfg, sid, digest_path,
                              effective_runs, existing=keep,
                              attempt=new_attempt)
    except (TaskEnqueueError, DispatchError):
        stats["errors"] += 1
        return
    if failed_typs:
        stats["retried"] += 1
    if missing_typs:
        stats["created"] += 1


def sweep_queue(engine: "Engine", wc: dict, cfg: dict, stats: dict,
                dry_run: bool) -> None:
    """Schritt 2: Digests in queue/ -- aelteste zuerst."""
    queue_dir = engine.digest_root / "queue"
    if not queue_dir.is_dir():
        return
    for digest_path in sorted(queue_dir.glob("*.digest.md")):
        sid = _sid_of_digest(digest_path)
        meta_path = queue_dir / f"{sid}.task.json"
        if not meta_path.exists():
            # Laeufe anlegen (Wrapper-Mechanik; [dispatch] ist die Quelle).
            if dry_run:
                stats["to_create"] += 1
                continue
            try:
                rows = load_dispatch_rows(cfg)
                runs = [{"session_typ": r["session_typ"],
                         "profil": r["profil"], "auto": r["auto"],
                         "ziel": _resolve_ziel(r, digest_path)}
                        for r in rows]
                _enqueue_curator_runs(engine, wc, cfg, sid, digest_path,
                                      runs, existing=None, attempt=1)
                stats["created"] += 1
            except (TaskEnqueueError, DispatchError):
                stats["errors"] += 1
            continue
        reconcile_digest(engine, wc, cfg, digest_path, meta_path, stats,
                         dry_run)
    # Orphans: task.json ohne Digest -> laut (Reparatur manuell).
    for meta_path in sorted(queue_dir.glob("*.task.json")):
        sid = meta_path.name.replace(".task.json", "")
        if not (queue_dir / f"{sid}.digest.md").exists():
            stats["errors"] += 1


def sweep_failed(engine: "Engine", wc: dict, cfg: dict, stats: dict,
                 dry_run: bool) -> None:
    """Schritt 3: Digests in failed/ -- chronologisch. Mit error.md =
    Endzustand; ohne error.md (Edge) -> Zustands-Pruefung wiederholen."""
    failed_dir = engine.digest_root / "failed"
    if not failed_dir.is_dir():
        return
    for digest_path in sorted(failed_dir.glob("*.digest.md")):
        sid = _sid_of_digest(digest_path)
        if (failed_dir / f"{sid}.error.md").exists():
            stats["failed_end"] += 1
            continue
        meta_path = failed_dir / f"{sid}.task.json"
        if meta_path.exists():
            reconcile_digest(engine, wc, cfg, digest_path, meta_path, stats,
                             dry_run)
        else:
            stats["errors"] += 1


def sweep_sessions(engine: "Engine", wc: dict, cfg: dict, stats: dict,
                   dry_run: bool) -> None:
    """Schritt 1: Erhaltungsgarantie ueber sessions/ -- unverdichtete Sessions
    nachverdichten (offene/verpasste werden spaetestens nachts geholt). Digest
    bereits bekannt -> idempotent ueberspringen (kein erneutes Verdichten)."""
    if not engine.sessions_root.is_dir():
        return
    for session_file in sorted(engine.sessions_root.rglob("*.jsonl")):
        sid = session_file.stem
        if _digest_known(engine, sid):
            stats["skipped"] += 1  # verarbeitet -- aenderungslos, vollwertig
            continue
        if dry_run:
            stats["to_catch_up"] += 1
            continue
        try:
            status, detail = digest_process_session(engine, wc, cfg,
                                                    session_file,
                                                    reason="sweep")
            if status == "processed":
                stats["catch_up"] += 1
                _ = detail
        except (DigestError, TaskEnqueueError, DispatchError):
            stats["errors"] += 1


def sweep(engine: "Engine", wc: dict, cfg: dict,
          dry_run: bool = False) -> dict:
    """Hauptlauf des Nacht-Sweeps: Session-Lage -> queue -> failed."""
    stats = {"catch_up": 0, "skipped": 0, "created": 0, "retried": 0,
             "done": 0, "escalated": 0, "failed_end": 0, "errors": 0,
             "auto": 0, "to_catch_up": 0, "to_create": 0, "to_retry": 0,
             "to_done": 0, "to_escalate": 0, "to_auto": 0}
    sweep_sessions(engine, wc, cfg, stats, dry_run)
    sweep_queue(engine, wc, cfg, stats, dry_run)
    sweep_failed(engine, wc, cfg, stats, dry_run)
    return stats


def format_sweep_report(stats: dict) -> str:
    """Sweep-Bericht (formatstabil, maschinenlesbar -- fuer systemd/hooks)."""
    parts = [f"catch_up={stats.get('catch_up', 0)}",
             f"skipped={stats.get('skipped', 0)}",
             f"created={stats.get('created', 0)}",
             f"retried={stats.get('retried', 0)}",
             f"done={stats.get('done', 0)}",
             f"auto={stats.get('auto', 0)}",
             f"escalated={stats.get('escalated', 0)}",
             f"failed_end={stats.get('failed_end', 0)}",
             f"errors={stats.get('errors', 0)}"]
    dry = {k: stats.get(k) for k in ("to_catch_up", "to_create", "to_retry",
                                     "to_done", "to_escalate", "to_auto")
           if stats.get(k)}
    if dry:
        parts.append("dry-run: " + ", ".join(f"{k}={v}" for k, v in dry.items()))
    return "sweep: " + " ".join(parts)

# ---------------------------------------------------------------------------

def _parse_records(raw_text: str) -> Tuple[List[dict], int, List[str]]:
    """Zeilen parsen mit Toleranz (P1): nicht-parsbare Zeilen werden
    GEZÄHLT und laut ausgewiesen — nichts wird still verworfen.

    Rückgabe: (Records, parse_fehler_count, format_version_warnungen)."""
    records: List[dict] = []
    parse_errors = 0
    versions: List[str] = []
    for raw in raw_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
            for key in ("version", "protocolVersion"):
                if key in obj and obj[key] not in (None, 1, "1", 3, "3"):
                    versions.append(str(obj[key]))
    warns = sorted({f"format_version={v} (unerwartet)" for v in versions})
    return records, parse_errors, warns


def _head_chain(records: List[dict]) -> List[dict]:
    """§9.2 — Head-Chain-Auflösung: Pi-Session-Format ist ein Record-Baum
    (id/parentId); nur die HEAD-Kette ist gültig — verworfene Zweige
    (Regenerate) fliegen heraus. Flache Dateien (ohne id/parentId) bleiben
    linear."""
    with_id = [r for r in records if isinstance(r.get("id"), str)]
    if not with_id:
        return records
    by_id = {r["id"]: r for r in with_id}
    referenced = {r["parentId"] for r in with_id
                  if isinstance(r.get("parentId"), str)}
    heads = [r for r in with_id if r["id"] not in referenced]
    if not heads:
        return with_id  # degeneriert (Zyklus?) — laut, linear
    head = max(heads, key=lambda r: str(r.get("timestamp") or ""))
    chain: List[dict] = []
    cur: Optional[dict] = head
    seen_ids = set()
    while cur is not None:
        chain.append(cur)
        pid = cur.get("parentId")
        if not isinstance(pid, str) or pid in seen_ids:
            break
        seen_ids.add(pid)
        cur = by_id.get(pid)
    chain.reverse()
    return chain


def _turn_text(obj: dict) -> Tuple[Optional[str], bool]:
    """B2 — Turn-Text extrahieren: User-Turns und Assistant-Texte vollständig
    (kein Snip); Thinking-Items eliminiert; Tool-Args/Results eliminiert
    (B5). Rückgabe: (Text|None, ist_turn)."""
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None, False
    content = msg.get("content")
    parts: List[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
            # tool_use / tool_result / thinking → eliminiert (B2/B5)
    text = "\n".join(p for p in parts if p and p.strip())
    return (text or None), True


def _engine_topic_signals(cmd: str) -> List[str]:
    """B1 — Engine-Verben mit Topic-Argument extrahieren (in Reihenfolge)."""
    hits: List[str] = []
    for fname in ENGINE_FILENAMES:
        if fname not in cmd:
            continue
        for verb in B1_VERBS:
            if verb not in cmd:
                continue
            m = re.search(rf"{verb}\b.*?--topic\s+([A-Za-z0-9_-]+)", cmd)
            if m:
                hits.append(m.group(1))
                continue
            m = re.search(rf"\"{verb}\"[^}}]*?\"topic\"\s*:\s*"
                          r"\"([A-Za-z0-9_-]+)\"", cmd)
            if m:
                hits.append(m.group(1))
    return hits


def _sockel_topic_signal(path: str) -> List[str]:
    """B1 — Sekundär-Signal aus dem Pfad eines read-Calls:
    ``topics/<name>/sockel.md`` → ``<name>`` (Datei-Lese-Pfad zum
    Topic-Stand). Signal-Quelle ist der Pfad-Parameter, nicht der Text —
    Doku-/Template-Zitate im Text triggern nicht (Fix 2026-09-24)."""
    m = _SOCKEL_TOPIC_RE.search(path)
    return [m.group(1)] if m else []


def _topic_signals_from_chain(chain: List[dict]) -> List[str]:
    """B1 — Engine-Topic-Signale in chain-Reihenfolge (erstes Vorkommen
    zählt → first_topic): (1) bash-Kommandos mit CLI-Verb
    (pib topic read/create --topic X) → _engine_topic_signals;
    (2) read-Calls auf topics/<name>/sockel.md → _sockel_topic_signal.
    Der in der Praxis übliche Weg — Topic-Stand per read-Tool laden —
    liefert damit ebenfalls ein Signal (Defekt-Fund
    audit/PHASE4-B1-SIGNAL-ERKENNUNG.md, Fix 2026-09-24)."""
    hits: List[str] = []
    for obj in chain:
        msg = obj.get("message") if isinstance(obj.get("message"), dict) \
            else obj
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for c in content:
            if not (isinstance(c, dict)
                    and c.get("type") in ("tool_use", "toolCall")):
                continue
            name = c.get("name")
            # v1/v2: "input" · v3 (pi ≥ 0.87): "arguments"
            inp = c.get("input") or c.get("arguments") or {}
            if name == "bash":
                cmd = inp.get("command")
                if isinstance(cmd, str):
                    for t in _engine_topic_signals(cmd):
                        if t not in hits:
                            hits.append(t)
            elif name == "read":
                p = str(inp.get("path") or inp.get("file_path") or "")
                for t in _sockel_topic_signal(p):
                    if t not in hits:
                        hits.append(t)
    return hits


def _is_write_cmd(cmd: str) -> bool:
    """B3 — Heuristik: bash-Tokens mit schreibender Wirkung."""
    if REDIRECT_RE.search(cmd):
        return True
    if SED_I_RE.search(cmd):
        return True
    tokens = TOKEN_SPLIT_RE.split(cmd)
    return any(t in WRITE_TOKENS for t in tokens if t)


def _collect_artifacts(chain: List[dict]) -> Tuple[List[dict], List[str]]:
    """B3 — Artefakte aus der Head-Chain: write/edit-Calls + bash-Kommandos
    mit schreibender Wirkung. Nur Pfad/Aktion/Erfolg/Größe — keine
    Dateiinhalte. Fehlversuche als Eintrag mit Erfolgs-Flag."""
    # 1. Pass: tool_use sammeln (id → Artefakt), bash-Kommandos extrahieren.
    tools: Dict[str, dict] = {}
    bash_commands: List[str] = []
    for obj in chain:
        msg = obj.get("message") if isinstance(obj.get("message"), dict) \
            else obj
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not (isinstance(c, dict)
                    and c.get("type") in ("tool_use", "toolCall")):
                continue
            name = c.get("name")
            # v1/v2: "input" · v3 (pi ≥ 0.87): "arguments"
            inp = c.get("input") or c.get("arguments") or {}
            tid = c.get("id")
            if name in ("write", "edit"):
                path = str(inp.get("path") or inp.get("file_path") or "?")
                size = 0
                for k in ("content", "newText", "text"):
                    v = inp.get(k)
                    if isinstance(v, str):
                        size = max(size, len(v))
                rec = {"typ": name, "pfad": path, "ok": True,
                       "groesse": f"{size} B"}
                tools[tid] = rec
            elif name == "bash":
                cmd = inp.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    bash_commands.append(cmd)
                    if _is_write_cmd(cmd):
                        tools[tid] = {"typ": "bash-write", "pfad": cmd[:200],
                                      "ok": True, "groesse": "—"}
    # 2. Pass: tool_result paaren (Erfolgs-Flag; Fehlversuche bleiben).
    #    v1/v2: tool_result-Content-Items · v3: Message-Records mit
    #    role="toolResult" + toolCallId + isError.
    for obj in chain:
        msg = obj.get("message") if isinstance(obj.get("message"), dict) \
            else obj
        if isinstance(msg, dict) and msg.get("role") == "toolResult":
            ref = msg.get("toolCallId")
            if ref in tools and (msg.get("is_error") or msg.get("isError")):
                tools[ref]["ok"] = False
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                ref = c.get("tool_use_id")
                if ref in tools and (c.get("is_error")
                                     or c.get("isError")):
                    tools[ref]["ok"] = False
    arts = list(tools.values())
    return arts, bash_commands


def _bash_stats(commands: List[str], project_path: Optional[str]) -> dict:
    """B3 — Statistik: gesamt · write · in-scope · ausserhalb · blacklist.
    Blacklist hat Vorrang vor Scope."""
    stats = {"total": len(commands), "write": 0, "write_in_scope": 0,
             "write_out_of_scope": 0, "blacklist": 0}
    proj = str(project_path) if project_path else None
    for cmd in commands:
        if not _is_write_cmd(cmd):
            continue
        stats["write"] += 1
        if BLACKLIST_RE.search(cmd):
            stats["blacklist"] += 1
            continue
        if proj and proj in cmd:
            stats["write_in_scope"] += 1
        else:
            stats["write_out_of_scope"] += 1
    return stats


def render_digest(engine: "Engine", sid: str, raw_text: str,
                  warn_budget_kib: int = DEFAULT_WARN_BUDGET_KIB) -> str:
    """Digest-Rendering (rein deterministisch): B1–B5 + Kopf nach §9.2.

    * SID-Vollform-Kopfzeile (Beleg + Nachverfolgung)
    * Statistik-Zeile `**Statistik:** N Turns` (exaktes Gate-Format, §11)
    * Parse-Toleranz laut im Kopf · Format-Versions-Warnung
    * B2 Conversation vollständig · B3 Artefakte (Pfad/Aktion/Erfolg/Größe)
    * B4 vollständiger committeter `sockel.md`-Stand des Haupt-Topics +
      vollständiger Domänen-Stand (`domains/*.md` — Domain-Lauf-Grundlage,
      Festzurrung 2026-09-24)
    * Größenbudget als Kopf-Metrik + `markierungen` (Warn-Transport, §9.2)
    """
    records, parse_errors, version_warns = _parse_records(raw_text)
    chain = _head_chain(records)

    turns: List[Tuple[int, str, Optional[str]]] = []
    first_ts: Optional[str] = None
    cwd: Optional[str] = None
    for obj in chain:
        if first_ts is None:
            ts = obj.get("timestamp") or (obj.get("message") or {}).get(
                "timestamp")
            if ts:
                first_ts = str(ts)
        if isinstance(obj.get("cwd"), str):
            cwd = obj["cwd"]
        text, is_turn = _turn_text(obj)
        if is_turn:
            turns.append((len(turns) + 1, (obj.get("message") or obj).get(
                "role", "?"), text))

    arts, bash_commands = _collect_artifacts(chain)
    engine_topics = _topic_signals_from_chain(chain)

    first_topic = engine_topics[0] if engine_topics else None
    sockel_block: Optional[str] = None
    if first_topic:
        try:
            lt = engine.read_topic(first_topic)
            sockel_block = (f"## Sockel (First-Topic: {first_topic})\n\n"
                            f"{lt['sockel'].rstrip()}")
        except EngineError:
            sockel_block = (f"## Sockel (First-Topic: {first_topic})\n\n"
                            "(Sockel nicht lesbar — Topic existiert "
                            "vielleicht noch nicht.)")

    # B4 — vollständiger Domänen-Stand (alle domains/*.md): Domain-Lauf-
    # Grundlage (Liste + Inhalt für Nicht-Redundanz/Idempotenz,
    # Festzurrung 2026-09-24).
    dom_block: Optional[str] = None
    dom_files = (sorted(engine.domains_path.glob("*.md"))
                 if engine.domains_path.is_dir() else [])
    if dom_files:
        dom_parts: List[str] = []
        for dom_file in dom_files:
            dom_parts.append(
                f"### Domäne: {dom_file.stem} ({dom_file.resolve()})\n\n"
                f"{dom_file.read_text(encoding='utf-8').rstrip()}")
        dom_block = ("## Domänen-Stand (vollständig, alle domains/*.md)\n\n"
                     + "\n\n".join(dom_parts))

    stats = _bash_stats(bash_commands, cwd)
    n_turns = len(turns)

    L: List[str] = [f"# Digest — {sid}",
                    f"- session_id: {sid}"]
    if first_ts:
        L.append(f"- date: {first_ts}")
    L.append(f"- first_topic: {first_topic or '(kein Engine-Signal)'}")
    if engine_topics:
        L.append(f"- engine_signals: {len(engine_topics)} "
                 f"({', '.join(engine_topics)})")
    L.append(f"- parse_fehler: {parse_errors} Zeilen (laut, P1)")
    for w in version_warns:
        L.append(f"- WARNUNG: {w}")
    L.append(f"**Statistik:** {n_turns} Turns")

    # B5 — Warn-Transport: Größenbudget als Kopf-Metrik (kein Fehlerfall).
    markierungen: List[str] = []
    body_est = sum(len(t) for _, _, t in turns if t) + len(raw_text) // 4
    if body_est > warn_budget_kib * 1024:
        markierungen.append("size_warn")
    L.append(f"- markierungen: {', '.join(markierungen) or '(keine)'}")
    L.append("")

    # B2 — Conversation vollständig (kein Snip).
    L.append("## Conversation (B2)")
    for i, role, text in turns:
        L.append(f"### turn {i} ({role})")
        L.append(text if text else "(kein Text — nur eliminierte Items, B5)")
        L.append("")

    # B4 — vollständiger committeter Sockel-Stand des Haupt-Topics.
    if sockel_block:
        L.append(sockel_block)
        L.append("")

    # B4 — vollständiger Domänen-Stand (Domain-Lauf-Grundlage, Festzurrung
    # 2026-09-24): Liste + Inhalt für Nicht-Redundanz/Idempotenz.
    if dom_block:
        L.append(dom_block)
        L.append("")

    # B3 — Artefakte: nur Pfad/Aktion/Erfolg/Größe, keine Inhalte.
    L.append("## Artefakte (B3)")
    if arts:
        for a in arts:
            L.append(f"- {a['typ']} {a['pfad']} "
                     f"{'ok' if a['ok'] else 'FAIL'} ({a['groesse']})")
    else:
        L.append("- (keine schreibenden Artefakte)")
    L.append("")
    L.append("## Statistik (B3)")
    L.append(f"- bash_commands: {stats['total']}")
    L.append(f"- write_ops: {stats['write']} "
             f"(in-scope: {stats['write_in_scope']},"
             f" ausserhalb_projekt: {stats['write_out_of_scope']},"
             f" blacklist: {stats['blacklist']})")

    # B5 — Warn-Metriken (kein Fehlerfall).
    warns: List[str] = []
    if stats["total"] > 200:
        warns.append(f"bash_commands={stats['total']} (hoch)")
    if stats["write_out_of_scope"] > 20:
        warns.append(f"write_out_of_scope={stats['write_out_of_scope']}")
    if warns:
        L.append(f"- WARNUNGEN: {'; '.join(warns)}")

    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# pib doctor — Struktur-/Konfig-Prüfung (F21, §4)
# ---------------------------------------------------------------------------
# Prüft den Installationszustand laut und ratet nichts: Root-Verzeichnisse,
# INDEX.md, Digest-/Staging-Fächer, die zentrale Konfig (alle Sektionen) und
# die Instanz-Daten (Modell-Auflösbarkeit, Rollen-Templates). Exit 0 = grün;
# Exit != 0 = vollständige Lücken-Liste. Es ergänzt nie still etwas.

#: Erwartete Konfig-Sektionen (§3/§10). [dispatch] ist eine Tabelle oder ein
#: Array-of-Tables (`[[dispatch]]`), beides gilt als „vorhanden".
CONFIG_SECTIONS = ("paths", "memory", "worker", "roles", "models",
                   "dispatch", "trigger")

#: Struktur-Verzeichnisse (relativ zum Bundle-Root, CONCEPT §2).
STRUCTURE_DIRS = (
    "memory", "worker", "lib", "hooks",
    "memory/topics", "memory/domains",
    "memory/staging/topics", "memory/staging/_domains",
    "memory/staging/done", "memory/staging/rejected",
    "memory/digest/queue", "memory/digest/done", "memory/digest/failed",
    "worker/queue/pending", "worker/queue/running",
    "worker/queue/completed", "worker/queue/failed", "worker/output",
)

#: Struktur-Dateien (relativ zum Bundle-Root, CONCEPT §2).
STRUCTURE_FILES = ("memory/INDEX.md",)

#: Rollen-Template-Verzeichnis unterhalb des Bundle-Roots (Install-Ziel der
#: `roles/*.md`-Vorlagen; der Installer installiert sie dorthin, wo die
#: Konfig-Rollen sie erwarten).
ROLE_TEMPLATES_DIR = "roles"


def _doctor_structure(root: Path) -> List[str]:
    """Struktur-Check: Root-Verzeichnisse, INDEX.md, Digest-/Staging-Fächer."""
    gaps: List[str] = []
    for rel in STRUCTURE_DIRS:
        if not (root / rel).is_dir():
            gaps.append(f"Verzeichnis fehlt: {rel}/")
    for rel in STRUCTURE_FILES:
        if not (root / rel).is_file():
            gaps.append(f"Datei fehlt: {rel}")
    return gaps


def _doctor_config(root: Path) -> Tuple[Optional[dict], List[str]]:
    """Konfig-Check: config.toml vorhanden + alle Sektionen vollständig.
    Rückgabe: (cfg|None, gaps). Ist die Konfig unlesbar/fehlt sie, wird das
    laut gemeldet und alle Instanz-Checks übersprungen (kein Raten)."""
    cfg_path = root / CONFIG_NAME
    if not cfg_path.is_file():
        return None, [f"Konfig fehlt: {cfg_path}"]
    try:
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return None, [f"Konfig unlesbar: {cfg_path}: {e}"]
    if not isinstance(cfg, dict):
        return None, ["Konfig ist kein TOML-Objekt (config.toml)."]
    gaps: List[str] = []
    for sec in CONFIG_SECTIONS:
        if sec not in cfg or cfg.get(sec) is None:
            gaps.append(f"Konfig-Sektion fehlt: [{sec}]")
    return cfg, gaps


def _doctor_instance(root: Path, cfg: Optional[dict]) -> List[str]:
    """Instanz-Check: Modell-Auflösbarkeit ([roles.*].model und
    [worker].default_model gegen [models].available ∪ CLOUD) und
    Rollen-Templates vorhanden (ratet nichts)."""
    if cfg is None:
        return []
    gaps: List[str] = []
    models = cfg.get("models") or {}
    available = models.get("available")
    if not isinstance(available, list) or not all(
            isinstance(a, str) and a.strip() for a in available):
        gaps.append("[models].available fehlt oder ist keine String-Liste "
                    "(Whitelist der Modell-Aliase).")
        return gaps
    allowed = set(available) | {"CLOUD"}

    worker_t = cfg.get("worker") or {}
    default_model = worker_t.get("default_model")
    if not default_model:
        gaps.append("[worker].default_model fehlt.")
    elif default_model not in allowed:
        gaps.append(
            f"[worker].default_model={default_model!r} ist nicht in "
            f"[models].available ∪ CLOUD ({sorted(allowed)}).")

    roles = cfg.get("roles") or {}
    if not roles:
        gaps.append("[roles] definiert keine Rollen (mindestens eine "
                    "`[roles.<name>]`-Tabelle erwartet).")
    templates_dir = root / ROLE_TEMPLATES_DIR
    for name in sorted(roles):
        if not isinstance(roles[name], dict):
            gaps.append(f"[roles.{name}] ist keine Tabelle.")
            continue
        rm = roles[name].get("model")
        if rm:
            if not isinstance(rm, str):
                gaps.append(f"[roles.{name}].model ist kein String: {rm!r}.")
            elif rm not in allowed:
                gaps.append(
                    f"[roles.{name}].model={rm!r} ist nicht in "
                    f"[models].available ∪ CLOUD ({sorted(allowed)}).")
        if not (templates_dir / f"{name}.md").is_file():
            gaps.append(
                f"Rollen-Template fehlt: {templates_dir}/{name}.md "
                f"(für Rolle '{name}').")
    return gaps


def cmd_doctor(root: Path) -> int:
    """`pib doctor` — laut, ratet nichts; Exit 0 grün / Exit 1 mit Lücken."""
    gaps: List[str] = []
    gaps += _doctor_structure(root)
    cfg, cfg_gaps = _doctor_config(root)
    gaps += cfg_gaps
    gaps += _doctor_instance(root, cfg)

    print(f"pib doctor — Struktur-/Konfig-Prüfung (F21)")
    print(f"Root: {root}")
    print("")
    if gaps:
        print(f"Lücken ({len(gaps)}):")
        for g in gaps:
            print(f"  [FEHLT] {g}")
        print("")
        print("Ergebnis: NICHT grün — Instanz-Daten/Konfig nachziehen, "
              "dann erneut `pib doctor`.")
        return 1
    print("  [ok]   Struktur, Konfig und Instanz-Daten vollständig.")
    print("Ergebnis: grün.")
    return 0


# ---------------------------------------------------------------------------
# CLI (§4.7 — Exit-Codes: 0 ok · 1 Validierung/I/O · 2 Argumente)
# ---------------------------------------------------------------------------

def _emit_ok(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _err(msg: str, code: int) -> int:
    """Fehler laut auf stderr, strukturiert; Exit-Code zurück."""
    print(json.dumps({"status": "error", "message": str(msg)},
                     ensure_ascii=False, indent=2), file=sys.stderr)
    return code


def _build_engine(args) -> Engine:
    """Konfig laden + Engine aus dem Bundle-Root bauen (lauter Fehler bei
    fehlender/unlesbarer Konfig — kein Default-Raten auf Datenpfade)."""
    root = resolve_bundle_root(args.home)
    cfg = load_config(root)
    base = memory_root_from_config(root, cfg)
    sr = sessions_root_from_config(cfg)
    return Engine(base, sessions_root=sr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """pib CLI (Memory-Teil, Schritt 4a). Exit-Codes:
    0 ok · 1 Validierung/I-O · 2 Argumente/Konfig.

    Grammatik:
      pib topic read    --topic <n> [--provenienz] [--snapshot <ts>-<sid>]
      pib topic create  --name <n> --titel <t> --spielart <spielart>
                        [--projekt <abs>] [--beschreibung <d>]
      pib domain create --name <n> --titel <t> --entry-key <k>
      pib package commit --paket <abs> [--eintraege <id>]...
      pib package reject --paket <abs> [--eintraege <id>]... [--grund <t>]
      pib pipeline status
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="pib",
        description="pi-bundle — Memory-Engine CLI (Schritt 4a).")
    parser.add_argument("--home", default=None,
                        help=f"Bundle-Root überschreiben (sonst Env "
                             f"{ENV_BUNDLE_HOME} oder {DEFAULT_BUNDLE_HOME})")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- pib topic ... ----------------------------------------------------
    p_topic = sub.add_parser("topic", help="Memory-Topics")
    topic_sub = p_topic.add_subparsers(dest="action", required=True)

    p_read = topic_sub.add_parser("read", help="Lese-Vertrag (§4.1)")
    p_read.add_argument("--topic", required=True)
    p_read.add_argument("--provenienz", action="store_true", default=False,
                        help="On-demand: Provenienz-Tabelle laden "
                             "(Invariante 3 — nicht im Normal-Read)")
    p_read.add_argument("--snapshot", default=None, metavar="<ts>-<sid>",
                        help="On-demand: Snapshot statt Live-Stand laden "
                             "(Stelle-4-Rückblick, §5.2) + Snapshot-Liste")

    p_create = topic_sub.add_parser("create", help="Topic anlegen (§4.2)")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--titel", required=True)
    p_create.add_argument("--spielart", required=True, choices=SPIELARTEN)
    p_create.add_argument("--projekt", default=None)
    p_create.add_argument("--beschreibung", default=None)

    # ---- pib domain ... ------------------------------------------------------
    p_domain = sub.add_parser("domain", help="Domänen-Wissen (§5)")
    domain_sub = p_domain.add_subparsers(dest="action", required=True)

    p_domain_create = domain_sub.add_parser(
        "create", help="Domänen-Datei anlegen (ausdrücklicher Einzelakt)")
    p_domain_create.add_argument("--name", required=True)
    p_domain_create.add_argument("--titel", required=True)
    p_domain_create.add_argument("--entry-key", required=True,
                                 dest="entry_key",
                                 help="Entry-Schlüssel-Typ der Domänen-Datei "
                                      "(Meta-Header <!-- entry-key: … -->)")

    # ---- pib package ... ----------------------------------------------------
    p_package = sub.add_parser("package", help="Staging-Pakete")
    package_sub = p_package.add_subparsers(dest="action", required=True)

    p_commit = package_sub.add_parser("commit", help="Paket committen (§4.3)")
    p_commit.add_argument("--paket", required=True,
                          help="Absoluter Pfad zur Paket-Datei (.proposal.json)")
    p_commit.add_argument("--eintraege", action="append", default=None,
                          dest="eintrag", metavar="ID",
                          help="Teilmenge: intra-Paket-ID (wiederholbar); "
                               "weggelassen = ganzes Paket")

    p_reject = package_sub.add_parser("reject", help="Paket verwerfen (§4.4)")
    p_reject.add_argument("--paket", required=True,
                          help="Absoluter Pfad zur Paket-Datei (.proposal.json)")
    p_reject.add_argument("--eintraege", action="append", default=None,
                          dest="eintrag", metavar="ID",
                          help="Teilmenge: intra-Paket-ID (wiederholbar); "
                               "weggelassen = ganzes Paket")
    p_reject.add_argument("--grund", default=None)

    # ---- pib pipeline ... ----------------------------------------------------
    p_pipeline = sub.add_parser("pipeline", help="Pipeline-Rückstände (§10.4)")
    pipeline_sub = p_pipeline.add_subparsers(dest="action", required=True)
    pipeline_sub.add_parser("status", help="Rückstände ausgeben")

    # ---- pib digest ... (Verdrahtung, Schritt 4c) -----------------------------
    p_digest = sub.add_parser("digest", help="Verdrahtung (§9): Digest run/sweep")
    digest_sub = p_digest.add_subparsers(dest="action", required=True)

    p_run = digest_sub.add_parser("run",
                                  help="Digest bauen + curator-Tasks je "
                                       "[dispatch]-Zeile einreihen")
    p_run.add_argument("--session", required=True,
                       help="Absoluter Pfad zur Session-JSONL")

    p_sweep = digest_sub.add_parser("sweep",
                                    help="Nacht-Sweep (Erhaltungsgarantie + "
                                         "Buchhaltung + Auto-Commit)")
    p_sweep.add_argument("--dry-run", action="store_true",
                         help="Nur Bericht, keine Writes (umgeht keinen "
                              "Schutz — rein informativ)")

    # ---- pib task ... (Worker, Schritt 4b) ------------------------------------
    p_task = sub.add_parser("task", help="Worker-Tasks")
    task_sub = p_task.add_subparsers(dest="action", required=True)

    p_create = task_sub.add_parser("create",
                                   help="Task anlegen (atomares Staging)")
    p_create.add_argument("--role", required=True,
                          help="Worker-Rolle (aus [roles.*])")
    p_create.add_argument("--task", required=True,
                          help="Auftrag als Einzeiler (HART-Limit max_task_chars)")
    p_create.add_argument("--spec", default=None,
                          help="Optionaler absoluter Konzept-Pfad (muss existieren)")
    p_create.add_argument("--deliverable", action="append", default=None,
                          metavar="PFAD",
                          help="Absoluter Deliverable-Pfad (wiederholbar)")
    p_create.add_argument("--model", default=None,
                          help="Modell-Alias oder CLOUD (Whitelist: [models] ∪ CLOUD)")
    p_create.add_argument("--timeout", type=int, default=None,
                          help="Timeout in Sekunden (Default: [worker].default_timeout_seconds)")
    p_create.add_argument("--base", default=None,
                          help="(nur Tests) Staging-Workspace überschreiben")

    p_status = task_sub.add_parser("status", help="Task-Status (read-only über die Queues)")
    p_status.add_argument("task_id")
    p_status.add_argument("--json", action="store_true", help="JSON-Ausgabe")

    p_validate = task_sub.add_parser("validate", help="Task-Definition validieren")
    p_validate.add_argument("task_file", help="Pfad zur Task-JSON-Datei")

    p_cleanup = task_sub.add_parser("cleanup", help="Alte Tasks aufräumen")
    p_cleanup.add_argument("--days", type=int, default=30,
                           help="Tasks älter als X Tage löschen")
    p_cleanup.add_argument("--dry-run", action="store_true",
                           help="Nur anzeigen, was gelöscht würde")
    p_cleanup.add_argument("--keep-completed", action="store_true",
                           help="completed/ behalten, nur failed/ aufräumen")

    # ---- pib watchdog ... (Worker-Runtime, Schritt 4b) -----------------------
    p_watchdog = sub.add_parser("watchdog", help="Watchdog-Runtime (Worker)")
    p_watchdog.add_argument("--once", action="store_true",
                            help="Nur einen Task, dann Exit")

    # ---- pib doctor ... (Struktur-/Konfig-Prüfung, F21) ----------------------
    sub.add_parser("doctor",
                   help="Struktur-/Konfig-Prüfung (laut, ratet nichts; "
                        "Exit 0 grün / != 0 mit Lücken-Liste)")

    args = parser.parse_args(argv)

    # doctor läuft bewusst VOR dem Engine-Bau: er prüft den Installations-
    # zustand und darf auch bei fehlender Konfig die Lücken vollständig
    # melden (ratet nichts, bricht nicht an der fehlenden Konfig ab).
    if args.command == "doctor":
        return cmd_doctor(resolve_bundle_root(args.home))

    # Engine aus der Konfig bauen (lauter Fehler bei fehlender Konfig).
    try:
        engine = _build_engine(args)
    except EngineError as e:
        return _err(e, 2)

    try:
        if args.command == "topic":
            if args.action == "read":
                _emit_ok(engine.read_topic(args.topic,
                                           provenienz=args.provenienz,
                                           snapshot=args.snapshot))
            elif args.action == "create":
                _emit_ok(engine.create_topic(
                    name=args.name, titel=args.titel, spielart=args.spielart,
                    projekt=args.projekt, beschreibung=args.beschreibung))
        elif args.command == "domain":
            if args.action == "create":
                _emit_ok(engine.create_domain(
                    name=args.name, titel=args.titel,
                    entry_key=args.entry_key))
        elif args.command == "package":
            paket = Path(args.paket)
            if not paket.is_absolute():
                return _err("Paket-Pfad muss absolut sein: "
                            f"{args.paket!r}", 2)
            if args.action == "commit":
                _emit_ok(engine.commit_package(paket, args.eintrag))
            elif args.action == "reject":
                _emit_ok(engine.reject_package(paket, args.eintrag,
                                               grund=args.grund))
        elif args.command == "pipeline" and args.action == "status":
            _emit_ok(engine.pipeline_zaehler())
        elif args.command == "digest":
            return _cmd_digest(args)
        elif args.command == "task":
            return _cmd_task(args)
        elif args.command == "watchdog":
            return _cmd_watchdog(args)
    except EngineError as e:
        return _err(e, 1)
    except (OSError, json.JSONDecodeError) as e:
        return _err(f"I/O-Fehler: {e}", 1)
    return 0


def _worker_wc(args) -> dict:
    """Worker-Konfig aus dem Bundle-Root laden (lauter Fehler bei Config-Problem)."""
    try:
        return worker.load_worker_config(getattr(args, "home", None))
    except worker.ConfigError as e:
        raise EngineError(str(e))


def _cmd_task(args) -> int:
    """Dispatcher für `pib task create|status|validate|cleanup` (Worker, 4b)."""
    wc = _worker_wc(args)
    if args.action == "create":
        if args.timeout is None:
            args.timeout = wc["default_timeout_sec"]
        code, msg = worker.cmd_create(wc, args)
        if code == 0:
            print(msg, flush=True)
            return 0
        print(msg, file=sys.stderr)
        return 1
    if args.action == "status":
        code, out = worker.task_status(wc, args.task_id, as_json=args.json)
        print(out, flush=True)
        return code
    if args.action == "validate":
        code, msg = worker.cmd_validate(wc, args.task_file)
        if code == 0:
            print(msg, flush=True)
            return 0
        print(f"FAIL: {msg}", file=sys.stderr)
        return 1
    if args.action == "cleanup":
        prefix = "[DRY RUN] " if args.dry_run else ""
        print(f"{prefix}Cleanup | days={args.days} | keep_completed={args.keep_completed}")
        deleted = worker.cleanup_queue(wc, args.days, args.dry_run,
                                       args.keep_completed)
        if deleted == 0:
            print("  Keine alten Tasks gefunden.")
        else:
            print(f"\n  {deleted} Task(s) "
                  f"{'gekennzeichnet' if args.dry_run else 'gelöscht'}.")
        return 0
    raise EngineError(f"Unbekannte task-Aktion: {args.action}")


def _cmd_watchdog(args) -> int:
    """`pib watchdog [--once]` — Worker-Runtime (ein CLI, F20)."""
    wc = _worker_wc(args)
    return worker.cmd_watchdog(wc, once=args.once)


def _cmd_digest(args) -> int:
    """`pib digest run --session <abs>` / `pib digest sweep [--dry-run]` —
    die Verdrahtung (§9, Schritt 4c). Verdichtung läuft als curator-Rolle auf
    derselben Queue/Infrastruktur wie alle Tasks (F17), kein Sonderpfad."""
    root = resolve_bundle_root(args.home)
    cfg = load_config(root)
    base = memory_root_from_config(root, cfg)
    sr = sessions_root_from_config(cfg)
    engine_instance = Engine(base, sessions_root=sr)
    try:
        wc = worker.load_worker_config(args.home)
    except worker.ConfigError as e:
        return _err(f"Worker-Konfig: {e}", 2)

    if args.action == "run":
        session_file = Path(args.session).expanduser()
        if not session_file.is_absolute():
            return _err("--session muss ein absoluter Pfad sein.", 2)
        status, detail = digest_process_session(
            engine_instance, wc, cfg, session_file)
        _emit_ok({"status": status, "detail": detail})
        return 0

    if args.action == "sweep":
        stats = sweep(engine_instance, wc, cfg, dry_run=args.dry_run)
        print(format_sweep_report(stats))
        return 0

    raise EngineError(f"Unbekannte digest-Aktion: {args.action}")


if __name__ == "__main__":
    sys.exit(main())
