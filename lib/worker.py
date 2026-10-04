#!/usr/bin/env python3
"""
pi-bundle — Worker-Kern (Port Schritt 4b)

Ein-File-Port des Worker-Kerns (Task-Format, atomares Staging, Validierung,
Queue-Schemata, Watchdog, Cleanup). Die Kern-Logik wird aus den Quellen 1:1
übernommen; der Refactor betrifft ausschließlich die Ränder:

  * Konfig-Quelle: `config.toml` (Sektionen `[worker]`, `[roles.*]`,
    `[models]`) statt einer veralteten Ein-Datei-JSON-Konfig.
  * pib-Grammatik: die CLI-Wrapper leben in `lib/pib.py`; dieses Modul
    stellt die Funktionen.
  * Generische Namen/Pfade: `worker/queue/*`, `worker/output/<task-id>/`
    unter dem Bundle-Root.

Task-Format (strukturiert, kein Freitext-Prompt):
  * role – Worker-Rolle (aus `[roles.*]` in der Konfig)
  * task – Einzeiler (HART-Limit ≤ max_task_chars)
  * spec – optionaler, existenter absoluter Konzept-Pfad
  * deliverables – wiederholbare absolute Pfade (je eigene DELIVERABLE:-Zeile)
  * model – Modell-Alias oder `CLOUD` (Whitelist: `[models]` ∪ CLOUD)
  * timeout – Sekunden

Modell-Auflösungskette (D-9):
    `--model` > `[roles.<role>].model` > `[worker].default_model`
  CLOUD bleibt erlaubter Wert. Ungültige Aliase werden am Validator-Gate
  (Whitelist) abgelehnt — die Mechanik statt Vertrauen.

Queue (file-basiert, serial — ein laufender Task):
  * Zustände pending → running → completed/failed, laut markiert.
  * Zustand ergibt sich aus dem Ordner, nicht aus dem JSON.
  * Atomares Staging: tmp → `worker/queue/pending/` via os.replace.

Watchdog (runtime-Daemon):
  * While-Loop mit Poll-Intervall aus `[worker].poll_seconds`, serial.
  * Modell-Load/Unload-Polling (optional via `[worker.router]`).
  * `--once`-Modus: ein Task, dann Exit.

Nur Python 3.11+ Stdlib, keine Dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
CONFIG_NAME = "config.toml"
ENV_BUNDLE_HOME = "PI_BUNDLE_HOME"
DEFAULT_BUNDLE_HOME = "~/.pi/pi-bundle"

QUEUES = ("pending", "running", "completed", "failed")

#: CLOUD bleibt als Task-Modellwert ein fester Marker (Datenhygiene);
#: der Name des lokalen Providers im pi-Modell-Pool ist dagegen
#: instanzspezifisch (NF2) und kommt aus ``[worker].provider``.
PROVIDER_CLOUD = "CLOUD"
DEFAULT_CLOUD_MODEL = "cloud"  # Platzhalter, sofern Konfig nicht gesetzt

#: Modell-Gate: vom Config-`valid_roles` abhängig, aber die Worker-Rollen,
#: die IMMER ein Deliverable-Ziel verlangen, werden per `[worker]
#: deliverable_pflicht_roles` konfiguriert.
DEFAULT_DELIVERABLE_PFLICHT_ROLES: Tuple[str, ...] = ()

ROUTER_HTTP_TIMEOUT = 10   # s — ein einzelner Router-HTTP-Call
LOAD_POLL_INTERVAL = 5     # s — loading→loaded
UNLOAD_POLL_INTERVAL = 2   # s — loaded→unloaded


class WorkerError(Exception):
    """Fehler der Worker-Schicht (Konfig, I/O)."""


class ConfigError(WorkerError):
    """Fehler in der zentralen Konfig (config.toml) — fail-fast, kein Raten."""


class ModelResolutionError(WorkerError):
    """`legacy_or_invalid_model`: Task-Modell ist weder CLOUD noch Whitelist-Alias."""


class ValidationError(WorkerError):
    """Erste Regelverletzung eines Tasks — mit klarer Ursache."""


class RouterError(WorkerError):
    """Router-Fehler mit Fehlerklasse.

    Klassen: router_unreachable · model_not_in_pool · oom_on_load ·
             load_aborted · other_load_error
    """

    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class


# ---------------------------------------------------------------------------
# Konfig-Loader (config.toml → normalisiertes Worker-Dict)
# ---------------------------------------------------------------------------

def resolve_bundle_root(override: Optional[str] = None) -> Path:
    """Bundle-Root: `--home` > Env ``PI_BUNDLE_HOME`` > Default."""
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_BUNDLE_HOME)
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_BUNDLE_HOME).expanduser()


def _table(cfg: dict, key: str) -> dict:
    """Liest eine TOML-Tabelle (default {}), mit Typ-Prüfung."""
    value = cfg.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] muss eine Tabelle sein, ist {type(value).__name__}.")
    return value


def _req_int(tbl: dict, key: str, *, default: int, minval: int = 0) -> int:
    value = tbl.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= minval:
        raise ConfigError(f"[{key}] muss ein int > {minval} sein, ist {value!r}.")
    return value


def _req_str(tbl: dict, key: str, *, default: Optional[str] = None) -> Optional[str]:
    value = tbl.get(key, default)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"[{key}] muss ein String sein, ist {type(value).__name__}.")
    if value is not None and not value.strip():
        raise ConfigError(f"[{key}] darf nicht leer sein.")
    return value


def _req_str_list(tbl: dict, key: str, *, default: Optional[List[str]] = None) -> List[str]:
    value = tbl.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"[{key}] muss eine nicht-leere Liste von Strings sein.")
    return [v.strip() for v in value]


def load_bundle_config(root: Path) -> dict:
    """config.toml aus dem Bundle-Root laden (laut, kein Default-Raten)."""
    cfg_path = Path(root).expanduser() / CONFIG_NAME
    if not cfg_path.is_file():
        raise ConfigError(
            f"Konfig nicht gefunden: {cfg_path} — lege eine config.toml an "
            f"(Vorlage: config.example.toml) oder setze {ENV_BUNDLE_HOME}."
        )
    try:
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Konfig unlesbar (config.toml): {e}")
    if not isinstance(cfg, dict):
        raise ConfigError("Konfig ist kein TOML-Objekt (config.toml).")
    return cfg


def worker_config(root: Path, cfg: dict) -> dict:
    """Normalisiert die config.toml zu einem Worker-Konfig-Dict (eine Quelle).

    Erwartete Sektionen:
      [worker]            provider, poll_seconds, default_model,
                          default_timeout_seconds, max_task_chars,
                          deliverable_pflicht_roles
      [worker.router]     optional: base_url, load_timeout_sec,
                          unload_stop_timeout_sec, retry_backoff_sec
      [roles.<name>]      tools, wissensquellen, model (Modell-Alias je Rolle)
      [models]            available (Whitelist-Aliase), default (falls
                          [worker].default_model fehlt)

    Gibt ein Dict mit allen vom Worker-Kern benötigten Feldern zurück.
    """
    root = Path(root).expanduser().resolve()
    worker = _table(cfg, "worker")
    roles = _table(cfg, "roles")
    models = _table(cfg, "models")

    # --- Rollen (valid_roles aus den [roles.*]-Sektionen) ---
    valid_roles = sorted(roles.keys()) if roles else []
    if not valid_roles:
        raise ConfigError(
            "[roles] definiert keine Rollen — mindestens eine `[roles.<name>]` "
            "Tabelle wird für den Worker benötigt."
        )

    # --- Modell-Aliase (Whitelist) ---
    worker_models = _req_str_list(models, "available", default=[])
    if not worker_models:
        raise ConfigError(
            "[models].available ist leer — trage die Modell-Aliase der Instanz "
            "ein (Whitelist für Validate/Resolve)."
        )
    if len(worker_models) != len(set(worker_models)):
        raise ConfigError("[models].available enthält Duplikate.")

    # --- Standard-Modell (Auflösungskette-Fallback) ---
    default_model = _req_str(worker, "default_model") or _req_str(models, "default")
    if not default_model:
        raise ConfigError(
            "[worker].default_model (oder [models].default) muss gesetzt sein."
        )
    if default_model not in worker_models:
        raise ConfigError(
            f"default_model {default_model!r} ist nicht in [models].available: "
            f"{worker_models}"
        )

    # --- Role-Models (je Rolle ein Alias, optional) ---
    role_models: Dict[str, str] = {}
    for name in valid_roles:
        m = _req_str(roles[name], "model")
        if m:
            if m not in worker_models:
                raise ConfigError(
                    f"[roles.{name}].model={m!r} ist nicht in [models].available."
                )
            role_models[name] = m

    # --- deliverable-Pflichtrollen ---
    deliverable_always = [r for r in _req_str_list(
        worker, "deliverable_pflicht_roles",
        default=list(DEFAULT_DELIVERABLE_PFLICHT_ROLES))]
    unknown = set(deliverable_always) - set(valid_roles)
    if unknown:
        raise ConfigError(
            f"[worker].deliverable_pflicht_roles enthält unbekannte Rollen: "
            f"{sorted(unknown)} (erlaubt: {valid_roles})"
        )

    # --- Provider (NF2 — instanzspezifischer Name im pi-Modell-Pool) ---
    provider = _req_str(worker, "provider")
    if not provider:
        raise ConfigError(
            "[worker].provider fehlt — Name des lokalen Providers im "
            "pi-Modell-Pool (instanzspezifisch, NF2; siehe "
            "~/.pi/agent/models.json der Instanz)."
        )

    # --- Cloud ---
    cloud_provider = _req_str(models, "cloud_provider", default="CLOUD")
    cloud_default_model = _req_str(models, "cloud_default_model",
                                   default=DEFAULT_CLOUD_MODEL)

    # --- Router (optional) ---
    router = _table(worker, "router")
    router_cfg: Optional[dict] = None
    if router:
        base_url = _req_str(router, "base_url")
        if not base_url or not base_url.startswith(("http://", "https://")):
            raise ConfigError("[worker.router].base_url muss eine http(s)-URL sein.")
        router_cfg = {
            "base_url": base_url,
            "load_timeout_sec": _req_int(router, "load_timeout_sec",
                                         default=120, minval=0),
            "unload_stop_timeout_sec": _req_int(router, "unload_stop_timeout_sec",
                                                default=30, minval=0),
            "retry_backoff_sec": _req_str_list(router, "retry_backoff_sec",
                                               default=[10, 30, 60]),
        }

    wc = {
        "bundle_root": root,
        "workspace": root / "worker",
        "valid_roles": valid_roles,
        "role_models": role_models,
        "deliverable_always_roles": deliverable_always,
        "max_task_chars": _req_int(worker, "max_task_chars",
                                   default=250, minval=0),
        "poll_interval_sec": _req_int(worker, "poll_seconds",
                                      default=5, minval=0),
        "default_timeout_sec": _req_int(worker, "default_timeout_seconds",
                                        default=3600, minval=0),
        "worker_models": worker_models,
        "default_model": default_model,
        "worker_provider": provider,
        "cloud_provider": cloud_provider,
        "cloud_default_model": cloud_default_model,
        "router": router_cfg,
        "system_prompt": _req_str(worker, "system_prompt") or _req_str(
            worker, "system_prompt_path"),
    }
    return wc


def load_worker_config(root_override: Optional[str] = None) -> dict:
    """Bundle-Root auflösen + config.toml laden + normalisieren (fail-fast)."""
    root = resolve_bundle_root(root_override)
    cfg = load_bundle_config(root)
    return worker_config(root, cfg)


# ---------------------------------------------------------------------------
# Task-Aufbau (Deterministisch, kein Freitext-Prompt)
# ---------------------------------------------------------------------------

def build_prompt(task: str, spec: Optional[str], deliverables: List[str]) -> str:
    """Baut den Worker-Prompt aus den strukturierten Feldern.

    Jede DELIVERABLE:-Zeile trägt exakt einen Pfad. `--spec` wird als eigene
    'Spec:'-Zeile vorangestellt (Verweis statt Duplikat).
    """
    lines = [task.strip()]
    if spec:
        lines.append("")
        lines.append(f"Spec: {spec}")
    if deliverables:
        lines.append("")
        for d in deliverables:
            lines.append(f"DELIVERABLE: {d}")
    return "\n".join(lines)


def resolve_model(role: str, model_arg: Optional[str], wc: dict) -> str:
    """Modell-Auflösung (D-9): --model > [roles.<role>].model > default_model.

    Ergebnis ist IMMER konkret (Alias oder 'CLOUD'). Die Whitelist-Prüfung
    macht das Validator-Gate (eine Quelle).
    """
    if model_arg:
        return model_arg
    return wc["role_models"].get(role) or wc["default_model"]


def generate_task_id() -> Tuple[str, str]:
    """Task-ID (Rolle + Timestamp mit Millisekunden) + created_at."""
    ts = datetime.now()
    tid = f"{ts.strftime('%Y%m%d-%H%M%S%f')[:-3]}"
    return tid, ts.isoformat()


# ---------------------------------------------------------------------------
# Validierung (eine Quelle der Wahrheit für die Regeln)
# ---------------------------------------------------------------------------
# DELIVERABLE-Deklarations-Konvention im generierten Prompt.
DELIVERABLE_ANY_RE = re.compile(r"(?i)^\s*DELIVERABLE\s*:\s*(\S+)\s*$", re.MULTILINE)
DELIVERABLE_RE = re.compile(r"(?i)^\s*DELIVERABLE\s*:\s*(/\S+)\s*$", re.MULTILINE)


def _deliverable_targets_in_prompt(prompt: str) -> List[str]:
    return [m.group(1) for m in DELIVERABLE_ANY_RE.finditer(prompt)]


def _collect_relative_paths(text: str) -> List[str]:
    """Findet relative Pfad-Indikatoren (Regel: alle Pfadangaben absolut)."""
    suspects = []
    for m in re.finditer(r"(?<![\w/])(?:(?:\.\.?/)+\S*|[\w.-]+/\.\./)", text):
        suspects.append(m.group(0))
    for m in re.finditer(r"~(?=/)[^\s'\"]*", text):
        suspects.append(m.group(0))
    return sorted(set(suspects))


def _allowed_models(wc: dict) -> str:
    allowed = set(wc["worker_models"]) | {"CLOUD"}
    return ", ".join(sorted(allowed))


def _validate_model(wc: dict, task: dict) -> None:
    """Modell-Gate (R-6): 'model' ist Pflichtfeld und ∈ Whitelist ∪ CLOUD."""
    model = task.get("model")
    if model is None:
        raise ValidationError(
            "Pflichtfeld 'model' fehlt im Task. Erlaubt: " + _allowed_models(wc))
    if not isinstance(model, str) or not model.strip():
        raise ValidationError(
            f"'model' muss ein nicht-leerer String sein (ist {type(model).__name__}). "
            "Erlaubt: " + _allowed_models(wc))
    allowed = set(wc["worker_models"]) | {"CLOUD"}
    if model not in allowed:
        raise ValidationError(
            f"model='{model}' ist ungültig. Erlaubt: " + _allowed_models(wc))
    # CLOUD-Sperre curator (Datenhygiene, aus Quellen übernommen): curator
    # läuft ausschließlich lokal, nie CLOUD.
    if task.get("role") == "curator" and model == "CLOUD":
        raise ValidationError(
            "Rolle 'curator' darf NIEMALS CLOUD nutzen (Datenhygiene) – "
            "nur lokale Alias-Aliase.")


def _validate_struct_fields(wc: dict, task: dict) -> None:
    """Strukturierte v3.0-Felder: task (Einzeiler) · spec · deliverables."""
    max_task_chars = wc["max_task_chars"]
    task_str = task.get("task")
    if not isinstance(task_str, str) or not task_str.strip():
        raise ValidationError(
            "Strukturfeld 'task' fehlt oder ist leer — Auftrag als Einzeiler übergeben.")
    if len(task_str.strip()) > max_task_chars:
        raise ValidationError(
            f"Strukturfeld 'task' ist {len(task_str.strip())} Zeichen lang "
            f"(Limit {max_task_chars}). Einzeiler-Regel HART überschritten – "
            "erstelle ein Konzept im Projektverzeichnis und verweise per 'spec'.")

    spec = task.get("spec")
    if spec is not None:
        if not isinstance(spec, str) or not spec.strip():
            raise ValidationError("'spec' darf nicht leer sein, wenn gesetzt.")
        spec = spec.strip()
        if not spec.startswith("/"):
            raise ValidationError(
                f"'spec' muss ein absoluter Pfad sein (beginnt mit '/'): {spec}")
        if not Path(spec).exists():
            raise ValidationError(
                f"'spec' zeigt auf einen nicht existierenden Pfad: {spec}. "
                "Das Konzept muss VOR der Taskerstellung im Projektverzeichnis "
                "angelegt werden.")

    deliverables = task.get("deliverables", []) or []
    if not isinstance(deliverables, list):
        raise ValidationError("'deliverables' muss ein Array absoluter Pfade sein.")
    seen = set()
    for d in deliverables:
        if not isinstance(d, str) or not d.strip():
            raise ValidationError("'deliverables' enthält einen leeren Eintrag.")
        d = d.strip()
        if not d.startswith("/"):
            raise ValidationError(
                f"DELIVERABLE-Ziel '{d}' ist kein absoluter Pfad (muss mit '/' beginnen).")
        if re.search(r"(^|/)output/", d, re.IGNORECASE):
            raise ValidationError(
                f"DELIVERABLE-Ziel '{d}' zeigt auf 'output/{{...}}/'. Der "
                "PROTOKOLL-Ordner ist ausschließlich für result/reflection/"
                "trace.md.")
        if d in seen:
            raise ValidationError(f"Duplikat-Deliverable: '{d}'.")
        seen.add(d)
    task["deliverables"] = list(seen)


def _validate_prompt_consistency(task: dict, prompt: str) -> None:
    deliverables = task.get("deliverables", []) or []
    prompt_targets = _deliverable_targets_in_prompt(prompt)
    if len(deliverables) != len(prompt_targets):
        raise ValidationError(
            f"Konsistenz-Fehler: 'deliverables' hat {len(deliverables)} Einträge, "
            f"der Prompt {len(prompt_targets)} DELIVERABLE:-Zeilen.")
    for d in deliverables:
        if d not in prompt_targets:
            raise ValidationError(
                f"Konsistenz-Fehler: Deliverable '{d}' fehlt als eigene "
                f"DELIVERABLE:-Zeile im Prompt.")


def validate_task_dict(wc: dict, task: dict) -> Tuple[bool, str]:
    """Validiert ein Task-Dict gegen alle harten Regeln (v3.0)."""
    valid_roles = set(wc["valid_roles"])
    role = task.get("role", "")
    if role not in valid_roles:
        raise ValidationError(
            f"role='{role}' ist ungültig. Erlaubt: {', '.join(sorted(valid_roles))}")

    _validate_model(wc, task)

    task_id = task.get("id", "")
    if not task_id:
        raise ValidationError("id fehlt im Task.")
    expected_output = f"output/{task_id}/"
    if task.get("output_path", "") != expected_output:
        raise ValidationError(
            f"output_path='{task.get('output_path')}' weicht von der Konvention "
            f"'{expected_output}' ab (PROTOKOLL-Ordner).")

    prompt = task.get("prompt", "")
    if not isinstance(prompt, str) or not prompt:
        raise ValidationError("Der generierte 'prompt' fehlt oder ist leer.")

    _validate_struct_fields(wc, task)

    rel = _collect_relative_paths(prompt)
    if rel:
        raise ValidationError(
            "Relative Pfade im Prompt – Pfadangaben müssen absolut sein:\n  "
            + "\n  ".join(repr(p) for p in rel))

    for target in _deliverable_targets_in_prompt(prompt):
        if re.search(r"(^|/)output/", target, re.IGNORECASE):
            raise ValidationError(
                f"DELIVERABLE-Ziel '{target}' zeigt auf 'output/{{...}}/'. "
                "Protokoll != Deliverable.")

    _validate_prompt_consistency(task, prompt)

    deliverable_declared = bool(DELIVERABLE_RE.search(prompt))
    if role in wc["deliverable_always_roles"] and not deliverable_declared:
        raise ValidationError(
            f"Rolle '{role}' erfordert IMMER ein Deliverable-Ziel. "
            "Füge mindestens ein --deliverable hinzu.")

    return True, f"OK: {task_id} – {role}"


def validate_task_file(wc: dict, task_path: Path) -> Tuple[bool, str]:
    """Validiert ein Task-File (Standalone-Diagnose)."""
    try:
        data = json.loads(task_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValidationError(f"Ungültiges JSON im Task-File: {e}")
    if not isinstance(data, dict):
        raise ValidationError("Task-File ist kein JSON-Objekt.")
    return validate_task_dict(wc, data)


# ---------------------------------------------------------------------------
# Task-Erstellung (atomares Staging)
# ---------------------------------------------------------------------------

def create_task(wc: dict, *, role: str, task: str, spec: Optional[str],
                deliverables: List[str], model: Optional[str], timeout: int,
                base: Optional[str] = None,
                extra_fields: Optional[dict] = None) -> Tuple[bool, str, str]:
    """Validiert, baut den Prompt, validiert erneut gg. Valiator und stagt.

    ATOMAR: erst wenn alle Prüfungen (Einzeiler, --spec-Existenz,
    Deliverable-Regeln, Validator-ok) bestanden sind, wird der Task nach
    `worker/queue/pending/` geschrieben (tmp → os.replace).

    Returns (ok, message, task_id).
    """
    if role not in set(wc["valid_roles"]):
        return False, (
            f"Unbekannte Rolle: {role}. Gültig: {', '.join(sorted(wc['valid_roles']))}"), ""

    task = task.strip()
    if not task:
        return False, "Der Auftrag (--task) darf nicht leer sein.", ""
    if len(task) > wc["max_task_chars"]:
        return False, (
            f"Auftrag zu lang: {len(task)} Zeichen (Limit {wc['max_task_chars']}). "
            "HART abgelehnt – erstelle ein minimales Konzept, übergebe es als "
            "--spec und kürze --task auf den Einzeiler."), ""

    if spec is not None:
        spec = spec.strip()
        if not spec.startswith("/"):
            return False, f"--spec muss ein absoluter Pfad sein (beginnt mit '/'): {spec}", ""
        if not Path(spec).exists():
            return False, (
                f"--spec zeigt auf einen nicht existierenden Pfad: {spec}. "
                "Das Konzept muss VOR der Taskerstellung angelegt werden."), ""

    seen = set()
    for d in deliverables:
        d = d.strip()
        if not d.startswith("/"):
            return False, f"--deliverable muss ein absoluter Pfad sein (beginnt mit '/'): {d}", ""
        if re.search(r"(^|/)output/", d, re.IGNORECASE):
            return False, (
                f"--deliverable zeigt auf 'output/{{...}}/': {d}. Der PROTOKOLL-"
                "Ordner ist ausschließlich für result.md/reflection.md/trace.md."), ""
        seen.add(d)
    deliverables = list(seen)

    ts = datetime.now()
    task_id = f"{role}-{ts.strftime('%Y%m%d-%H%M%S%f')[:-3]}"
    prompt = build_prompt(task, spec, deliverables)
    model_concrete = resolve_model(role, model, wc)

    task_json = {
        "id": task_id,
        "created_at": ts.isoformat(),
        "status": "pending",
        "role": role,
        "model": model_concrete,
        "task": task,
        "spec": spec,
        "deliverables": list(deliverables),
        "prompt": prompt,
        "output_path": f"output/{task_id}/",
        "timeout": timeout,
        "completed_at": None,
        "error": None,
    }

    if extra_fields:
        if not isinstance(extra_fields, dict):
            return False, "extra_fields muss ein dict sein.", ""
        unknown = set(extra_fields) - {"gate"}
        if unknown:
            return False, (
                f"extra_fields unterstützt nur 'gate' (unbekannt: "
                f"{', '.join(sorted(unknown))})."), ""
        task_json.update(extra_fields)

    try:
        ok, msg = validate_task_dict(wc, task_json)
    except ValidationError as e:
        return False, f"VALIDATOR-FAIL: {e}", ""
    if not ok:
        return False, f"VALIDATOR-FAIL: {msg}", ""

    workspace = Path(base).expanduser() if base else wc["workspace"]
    pending_dir = workspace / "queue" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_file = pending_dir / f"{task_id}.json"
    tmp = pending_file.with_suffix(".tmppending")
    try:
        tmp.write_text(json.dumps(task_json, indent=4), encoding="utf-8")
        os.replace(tmp, pending_file)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return False, f"Schreiben nach pending/ fehlgeschlagen: {e}", ""

    return True, f"{task_id} → pending (validiert, atomar eingereiht)", task_id


# ---------------------------------------------------------------------------
# Status (read-only über die Queues)
# ---------------------------------------------------------------------------

def find_task(workspace: Path, task_id: str) -> Tuple[Optional[str], Optional[Path]]:
    for queue in QUEUES:
        candidate = workspace / "queue" / queue / f"{task_id}.json"
        if candidate.exists():
            return queue, candidate
    return None, None


def load_task(task_path: Path) -> Optional[dict]:
    try:
        return json.loads(task_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def count_running(workspace: Path) -> int:
    return len(list((workspace / "queue" / "running").glob("*.json")))


def resolve_output_dir(workspace: Path, task: dict) -> Optional[Path]:
    raw = task.get("output_path") or task.get("output")
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = workspace / p
    return p


def check_artifacts(output_dir: Optional[Path]) -> dict:
    if output_dir is None or not output_dir.exists():
        return {"exists": False, "result_md": False, "reflection_md": False}
    return {
        "exists": True,
        "result_md": (output_dir / "result.md").exists(),
        "reflection_md": (output_dir / "reflection.md").exists(),
    }


def format_status_markdown(wc: dict, task_id: str, queue: str, task: dict) -> str:
    workspace = wc["workspace"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    role = task.get("role", "?")
    model = task.get("model", "WORKER")
    timeout = task.get("timeout", "n/a")
    created = task.get("created_at", "n/a")

    if queue == "pending":
        running = count_running(workspace)
        lines = [
            f"**Status:** PENDING – in der Warteschlange ({now})",
            f"**Rolle:** {role} | **Modell:** {model} | **Timeout:** {timeout}s",
            f"**Erstellt:** {created}",
        ]
        lines.append(
            f"**Watchdog:** siehe `pib watchdog` | **Tasks running:** {running} → "
            "dein Task wird nach FIFO (seriell) aufgenommen."
        )
        return "\n".join(lines)

    if queue == "running":
        return (
            f"**Status:** RUNNING – Task wird bearbeitet ({now})\n"
            f"**Rolle:** {role} | **Modell:** {model} | **Timeout:** {timeout}s\n"
            f"**Erstellt:** {created}\n"
            f"**Hinweis:** kein erneuter/verzögerter Check nötig. Nach Abschluss "
            f"liegt er in completed/ oder failed/."
        )

    if queue == "completed":
        arts = check_artifacts(resolve_output_dir(workspace, task))
        completed_at = task.get("completed_at", "n/a")
        error = task.get("error")
        lines = [
            f"**Status:** COMPLETED ({completed_at})",
            f"**Rolle:** {role} | **Modell:** {model}",
        ]
        if error:
            lines.append(f"**error-Feld:** {error} (ungewöhnlich bei completed)")
        if arts["exists"]:
            lines.append(
                f"**Protokoll:** result.md: "
                f"{'vorhanden' if arts['result_md'] else '❌ FEHLEND'}; "
                f"reflection.md: "
                f"{'vorhanden' if arts['reflection_md'] else '❌ FEHLEND'}"
            )
            if not arts["result_md"]:
                lines.append(
                    "⚠️ Anomalie: result.md fehlt — ein solcher Lauf würde als "
                    "failed verbucht (empty_result)."
                )
        else:
            lines.append(
                "⚠️ Output-Ordner nicht auffindbar: "
                f"{resolve_output_dir(workspace, task) or 'kein output_path'}"
            )
        return "\n".join(lines)

    if queue == "failed":
        error = task.get("error") or "(kein Fehler recorded)"
        completed_at = task.get("completed_at", "n/a")
        output_dir = resolve_output_dir(workspace, task)
        trace = f"{output_dir}/trace.md" if output_dir else "n/a"
        return (
            f"**Status:** FAILED ({completed_at})\n"
            f"**Rolle:** {role} | **Modell:** {model}\n"
            f"**Error:** {error}\n"
            f"**trace.md:** {trace}"
        )

    return "Unbekannter Zustand."


def format_status_json(wc: dict, task_id: str, queue: str, task: dict) -> str:
    payload = {
        "id": task_id,
        "queue": queue,
        "status": task.get("status"),
        "role": task.get("role"),
        "model": task.get("model"),
        "timeout": task.get("timeout"),
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
        "error": task.get("error"),
        "running_count": count_running(wc["workspace"]),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def task_status(wc: dict, task_id: str, as_json: bool = False) -> Tuple[int, str]:
    """Status eines Tasks. Returns (exit_code, output)."""
    queue, task_path = find_task(wc["workspace"], task_id)
    if queue is None or task_path is None:
        msg = (
            json.dumps({"error": "id nicht gefunden", "id": task_id}, ensure_ascii=False)
            if as_json else
            f"Task '{task_id}' in keiner Queue gefunden "
            f"({', '.join(QUEUES)}). Ungültige oder unbekannte id."
        )
        return 4, msg
    task = load_task(task_path)
    if task is None:
        return 1, f"❌ Task-Datei konnte nicht gelesen werden: {task_path}"
    out = format_status_json(wc, task_id, queue, task) if as_json \
        else format_status_markdown(wc, task_id, queue, task)
    return (0 if queue != "failed" else 2), out


# ---------------------------------------------------------------------------
# Queue-Mechanik (Watchdog)
# ---------------------------------------------------------------------------

def ensure_dirs(wc: dict) -> Path:
    workspace = wc["workspace"]
    for subdir in QUEUES:
        Path(workspace / "queue" / subdir).mkdir(parents=True, exist_ok=True)
    (workspace / "output").mkdir(parents=True, exist_ok=True)
    return workspace


def claim_next(wc: dict) -> Optional[Path]:
    pending_dir = wc["workspace"] / "queue" / "pending"
    running_dir = wc["workspace"] / "queue" / "running"

    if len(list(running_dir.glob("*.json"))) > 0:
        return None  # serial: max 1 Task gleichzeitig

    tasks = sorted(list(pending_dir.glob("*.json")),
                   key=lambda f: f.stat().st_mtime)
    for task_file in tasks:
        dest = running_dir / task_file.name
        try:
            task_file.rename(dest)  # atomic
            data = json.loads(dest.read_text())
            data["claimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            dest.write_text(json.dumps(data, indent=2))
            return dest
        except FileExistsError:
            continue
        except (json.JSONDecodeError, OSError):
            continue
    return None


def validate_claimed(wc: dict, task_path: Path) -> Tuple[bool, str]:
    """Lokale Claim-Prüfung (bewusst nicht das Validator-Modul — der Watchdog
    bleibt vom Gate-Skript entkoppelt)."""
    try:
        task = json.loads(task_path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return False, f"Ungültiges JSON: {e}"
    missing = [f for f in ("id", "role", "prompt") if f not in task]
    if missing:
        return False, f"Fehlende Felder: {', '.join(missing)}"
    if task["role"] not in set(wc["valid_roles"]):
        return False, f"Unbekannte Rolle: {task['role']}"
    return True, ""


def parse_role_tools(wc: dict, role: str) -> str:
    """Tools aus der Rolle (Konfig `[roles.<role>].tools`) — nie aus dem Task."""
    roles = load_bundle_config(wc["bundle_root"]).get("roles", {})
    tools = (roles.get(role) or {}).get("tools", [])
    if not isinstance(tools, list):
        return ""
    return ",".join(str(t) for t in tools)


def _verify_result(output_dir: Path) -> Tuple[bool, str]:
    """Post-Completion-Verifikation: result.md + reflection.md müssen existieren."""
    missing = [p for p in ("result.md", "reflection.md")
               if not (output_dir / p).exists()]
    if missing:
        return False, ("empty_result: Lauf endete mit rc=0, aber Protokoll-"
                       f"Artefakte fehlen: {', '.join(missing)}")
    return True, ""


# ---------------------------------------------------------------------------
# Modell-Load/Unload (optional via [worker.router])
# ---------------------------------------------------------------------------

def _router_request(wc: dict, method: str, path: str,
                    payload: Optional[dict] = None) -> Tuple[int, dict]:
    router = wc["router"]
    url = router["base_url"].rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=ROUTER_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, ValueError):
            body = {}
        return e.code, body
    except (urllib.error.URLError, OSError) as e:
        raise RouterError("router_unreachable", f"{method} {path}: {e}") from e
    except (json.JSONDecodeError, ValueError) as e:
        raise RouterError("router_unreachable",
                          f"{method} {path}: Antwort kein gültiges JSON: {e}") from e


def router_get_models(wc: dict) -> Dict[str, str]:
    code, body = _router_request(wc, "GET", "/models")
    if code != 200 or not isinstance(body.get("data"), list):
        raise RouterError("router_unreachable",
                          f"GET /models: HTTP {code}, unerwartete Antwort: {str(body)[:200]}")
    return {m["id"]: (m.get("status") or {}).get("value") for m in body["data"]}


def router_load(wc: dict, alias: str) -> None:
    router = wc["router"]
    code, body = _router_request(wc, "POST", "/models/load", {"model": alias})
    err = body.get("error") or {}
    if code == 200 and body.get("success"):
        pass
    elif code == 404 or err.get("type") == "not_found_error":
        raise RouterError("model_not_in_pool",
                          f"Alias {alias!r} nicht im Router-Pool (HTTP 404)")
    elif code == 400 and "already running" in str(err.get("message", "")):
        print(f"♨️  {alias} bereits loaded (HTTP 400 'already running') – als warm gewertet",
              flush=True)
        return
    else:
        msg = str(err or body)[:200]
        if "out of memory" in msg.lower() or "oom" in msg.lower():
            raise RouterError("oom_on_load", f"Load {alias!r}: OOM (HTTP {code}): {msg}")
        raise RouterError("other_load_error",
                          f"Load {alias!r} fehlgeschlagen (HTTP {code}): {msg}")

    deadline = time.time() + router["load_timeout_sec"]
    while time.time() < deadline:
        models = router_get_models(wc)
        status = models.get(alias)
        if status == "loaded":
            print(f"♨️  Load ok: {alias} (status=loaded)", flush=True)
            return
        if status == "unloaded":
            raise RouterError("load_aborted",
                              f"Load {alias!r} abgebrochen (Status zurück zu 'unloaded')")
        time.sleep(LOAD_POLL_INTERVAL)
    raise RouterError("load_aborted",
                      f"Load-Timeout: {alias} nach {router['load_timeout_sec']}s nicht 'loaded'")


def router_unload(wc: dict, alias: str) -> None:
    router = wc["router"]
    try:
        code, body = _router_request(wc, "POST", "/models/unload", {"model": alias})
        if code != 200:
            print(f"⚠️  Unload {alias!r}: HTTP {code} ({str(body)[:150]}) – weiter",
                  flush=True)
            return
    except RouterError as e:
        print(f"⚠️  Unload {alias!r}: Router nicht ansprechbar ({e.error_class}: {e}) – weiter",
              flush=True)
        return
    deadline = time.time() + router["unload_stop_timeout_sec"]
    while time.time() < deadline:
        try:
            if router_get_models(wc).get(alias) == "unloaded":
                print(f"❄️  Unload ok: {alias} (status=unloaded)", flush=True)
                return
        except RouterError as e:
            print(f"⚠️  Unload-Poll {alias!r}: {e.error_class} – weiter", flush=True)
            return
        time.sleep(UNLOAD_POLL_INTERVAL)
    print(f"⏳ unload_timeout: {alias} nach {router['unload_stop_timeout_sec']}s "
          f"nicht 'unloaded' (Force-Terminate ist Router-Aufgabe) – weiter", flush=True)


def ensure_loaded(wc: dict, alias: str) -> None:
    models = router_get_models(wc)
    if models.get(alias) == "loaded":
        print(f"♨️  {alias} bereits loaded – sofort warm", flush=True)
        return
    try:
        router_load(wc, alias)
        return
    except RouterError as e:
        if e.error_class != "oom_on_load":
            raise
        print(f"🔥 oom_on_load bei {alias!r} – entlade alle geladenen Modelle, Retry 1×",
              flush=True)
        for other, status in models.items():
            if status == "loaded" and other != alias:
                router_unload(wc, other)
        router_load(wc, alias)


def lookahead_same_alias(wc: dict, alias: str) -> bool:
    pending_dir = wc["workspace"] / "queue" / "pending"
    for task_file in sorted(pending_dir.glob("*.json"),
                            key=lambda f: f.stat().st_mtime):
        try:
            task = json.loads(task_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if task.get("model") == alias:
            return True
    return False


def boot_reconcile(wc: dict) -> None:
    if not wc["router"]:
        print("🧹 Boot-Reconcile: kein Router konfiguriert – übersprungen", flush=True)
        return
    router = wc["router"]
    backoffs = router["retry_backoff_sec"]
    attempt = 0
    while True:
        try:
            models = router_get_models(wc)
            break
        except RouterError as e:
            wait = backoffs[attempt % len(backoffs)]
            print(f"⚠️  Boot-Reconcile: Router unerreichbar ({e.error_class}) – "
                  f"warte {wait}s (Backoff-Schleife, kein Crash-Loop)", flush=True)
            time.sleep(wait)
            attempt += 1
    orphans = [a for a, s in models.items() if s == "loaded"]
    if not orphans:
        print("🧹 Boot-Reconcile: kein verwaistes Modell (Router-Pool leer)", flush=True)
        return
    for alias in orphans:
        print(f"🧹 Boot-Reconcile: {alias} geladen ohne Owner → entladen", flush=True)
        router_unload(wc, alias)


# ---------------------------------------------------------------------------
# pi-Aufruf & Task-Lifecycle
# ---------------------------------------------------------------------------

def resolve_task_model(wc: dict, task: dict) -> Tuple[str, str]:
    """Löst task['model'] zu (provider, model_id) auf – OHNE Fallback."""
    requested = task.get("model")
    if requested == PROVIDER_CLOUD:
        return PROVIDER_CLOUD, wc["cloud_default_model"]
    if isinstance(requested, str) and requested in set(wc["worker_models"]):
        return wc["worker_provider"], requested
    raise ModelResolutionError(
        f"legacy_or_invalid_model: Task-Modell {requested!r} ist kein gültiger "
        f"Alias – Task neu anlegen mit Alias (erlaubt: "
        f"{', '.join(wc['worker_models'])} oder 'CLOUD')")


def build_pi_cmd(wc: dict, task: dict, provider: str, model_id: str,
                 output_dir: Path) -> List[str]:
    """Baut den pi-Spawn-Befehl (Referenz-Mechanik).

    Default-Verhalten wie die Referenz:
      --system-prompt        <root>/roles/WORKER_SYSTEM.md
      --append-system-prompt <root>/roles/<role>.md   (immer, für jede Rolle)

    Der Config-Slot [worker].system_prompt ist ein **expliziter Override**:
    ersetzt nur den Basis-Prompt (--system-prompt); der Rollen-Append bleibt
    bestehen. Fehlt ein Template am erwarteten Ort → ConfigError (kein stiller
    Spawn ohne Rollen-Kontext).
    """
    role = task["role"]
    root = Path(wc["bundle_root"])

    # Basis-Prompt: Override [worker].system_prompt, sonst Default-Template.
    base = Path(wc["system_prompt"]) if wc["system_prompt"] else \
        root / "roles" / "WORKER_SYSTEM.md"
    role_template = root / "roles" / f"{role}.md"

    # Lauter Fehler statt stiller Spawn ohne Rollen-Kontext:
    if not base.is_file():
        raise ConfigError(
            f"Worker-Basis-Prompt fehlt: {base} (erwartet "
            f"<root>/roles/WORKER_SYSTEM.md oder [worker].system_prompt).")
    if not role_template.is_file():
        raise ConfigError(
            f"Rollen-Template fehlt: {role_template} (führt der Rolle "
            f"'{role}' den Rollen-Kontext zu; ohne ihn kein Spawn).")

    return [
        "pi", "-p",
        "--system-prompt", str(base),
        "--append-system-prompt", str(role_template),
        "--tools", parse_role_tools(wc, role),
        "--model", f"{provider}/{model_id}",
        "--session-dir", str(output_dir),
        "--no-session",
        task["prompt"],
    ]


def _book_task(wc: dict, task: dict, task_path: Path, status: str,
               error: Optional[str] = None) -> None:
    task["status"] = status
    if error:
        task["error"] = error
    dest = wc["workspace"] / "queue" / status / task_path.name
    dest.write_text(json.dumps(task, indent=2))
    task_path.unlink()
    if status == "completed":
        print(f"✅ Done: {task['id']}", flush=True)
    else:
        print(f"❌ Failed: {task['id']} – {(error or 'unbekannt')[:120]}", flush=True)


def run_task(wc: dict, task_path: Path) -> None:
    task = json.loads(task_path.read_text())
    task_id = task["id"]
    role = task["role"]
    timeout = task.get("timeout", wc["default_timeout_sec"])
    ts = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        provider, model_id = resolve_task_model(wc, task)
    except ModelResolutionError as e:
        task["completed_at"] = ts()
        _book_task(wc, task, task_path, "failed", str(e))
        return

    is_worker = provider == PROVIDER_WORKER
    if is_worker and wc["router"]:
        # Router-konfiguriert: Load best-effort mit Backoff. Kein Router →
        # Modell-Lifecycle übersprungen (Lauf startet direkt).
        try:
            ensure_loaded(wc, model_id)
        except RouterError as e:
            router_unload(wc, model_id)
            task["completed_at"] = ts()
            _book_task(wc, task, task_path, "failed", f"{e.error_class}: {e}")
            return

    output_dir = wc["workspace"] / task.get("output_path", f"output/{task_id}")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        cmd = build_pi_cmd(wc, task, provider, model_id, output_dir)
    except ConfigError as e:
        # Fehlendes Rollen-Template/-Basis-Prompt: laut fehlschlagen, aber den
        # Watchdog nicht crashen lassen (Task als failed buchen, weiter).
        task["completed_at"] = ts()
        _book_task(wc, task, task_path, "failed", str(e))
        return

    print(f"📦 Start: {task_id} [role={role}] model={provider}/{model_id} "
          f"timeout={timeout}s", flush=True)
    print(f"   CMD: {' '.join(cmd[:8])} ...", flush=True)

    status, error = "failed", None
    try:
        result = subprocess.run(cmd, cwd=str(output_dir), capture_output=True,
                                text=True, timeout=timeout)
        task["completed_at"] = ts()
        if result.returncode == 0:
            ok, reason = _verify_result(output_dir)
            if ok:
                status = "completed"
            else:
                error = reason
                print(f"⚠️  verifiziert: rc=0 aber kein Ergebnis → failed: {reason}",
                      flush=True)
        else:
            error = (result.stderr[-2000:] if result.stderr
                     else f"exit code {result.returncode}")

        stdout_log = output_dir / "stdout.log"
        if stdout_log.exists() or result.stdout:
            stdout_log.write_text(result.stdout)
    except subprocess.TimeoutExpired:
        task["completed_at"] = ts()
        error = f"Timeout nach {timeout}s"
        print(f"⏰ Timeout: {task_id} ({timeout}s)", flush=True)
    except Exception as e:
        task["completed_at"] = ts()
        error = str(e)
        print(f"💥 Error: {task_id} – {e}", flush=True)

    if is_worker and wc["router"]:
        if status == "completed" and lookahead_same_alias(wc, model_id):
            print(f"♨️  Keep-Warm: {model_id} (Look-Ahead: gleicher Alias pending)",
                  flush=True)
        else:
            router_unload(wc, model_id)

    _book_task(wc, task, task_path, status, error)


def check_running_stale(wc: dict) -> None:
    running_dir = wc["workspace"] / "queue" / "running"
    for task_file in running_dir.glob("*.json"):
        try:
            task = json.loads(task_file.read_text())
            claimed_at = task.get("claimed_at", "")
            if claimed_at:
                age = (datetime.now() - datetime.fromisoformat(claimed_at)).total_seconds()
                timeout = task.get("timeout", wc["default_timeout_sec"])
                if age > timeout * 2:
                    task["status"] = "failed"
                    task["error"] = f"Stale nach {int(age)}s (watchdog-Neustart)"
                    (wc["workspace"] / "queue" / "failed" / task_file.name
                     ).write_text(json.dumps(task, indent=2))
                    task_file.unlink()
                    print(f"⚠️ Stale: {task['id']} → failed/", flush=True)
        except Exception as e:
            print(f"⚠️ Prüffehler running/{task_file.name}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Cleanup (Task-Housekeeping)
# ---------------------------------------------------------------------------

def cleanup_queue(wc: dict, days: int, dry_run: bool,
                  keep_completed: bool) -> int:
    """Löscht alte completed/failed Tasks (+ Output-Ordner) nach Alter."""
    deleted = 0
    output = wc["workspace"] / "output"
    dirs = []
    if not keep_completed:
        dirs.append(wc["workspace"] / "queue" / "completed")
    dirs.append(wc["workspace"] / "queue" / "failed")

    cutoff = datetime.now() - timedelta(days=days)
    for dir_path in dirs:
        if not dir_path.exists():
            continue
        for task_file in dir_path.glob("*.json"):
            mtime = datetime.fromtimestamp(task_file.stat().st_mtime)
            if mtime < cutoff:
                task_id = task_file.stem
                output_dir = output / task_id
                if dry_run:
                    print(f"  WÜRDE löschen: {task_file.name}")
                    if output_dir.exists():
                        print(f"    + output/{task_id}/")
                else:
                    task_file.unlink()
                    if output_dir.exists():
                        shutil.rmtree(output_dir)
                    print(f"  ✅ {task_file.name}")
                deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# CLI-Einstiege (von pib.py verdrahtet; Watchdog auch direkt lauffähig)
# ---------------------------------------------------------------------------

def cmd_create(wc: dict, args) -> Tuple[int, str]:
    ok, message, _ = create_task(
        wc, role=args.role, task=args.task, spec=args.spec,
        deliverables=args.deliverable or [], model=args.model,
        timeout=args.timeout, base=args.base)
    return (0, f"OK: {message}") if ok else (1, message)


def cmd_validate(wc: dict, task_file: str) -> Tuple[int, str]:
    try:
        ok, msg = validate_task_file(wc, Path(task_file))
        return 0, msg
    except ValidationError as e:
        return 1, str(e)
    except Exception as e:
        return 1, f"unerwarteter Fehler: {e}"


def spawn_vector(wc: dict, role: str) -> List[str]:
    """Baut den pi-Spawn-Vektor für eine synthetische Diagnose-Task.

    Dient der Smoke-Paritäts-Prüfung: beide Prompt-Flags werden ohne echten
    pi-Spawn verifiziert (Default-Pfade, Override-Verhalten, Fehlverhalten bei
    fehlendem Template via ConfigError). Führt keinerlei pi-Aufruf aus.
    """
    if role not in wc["valid_roles"]:
        raise ConfigError(
            f"Rolle '{role}' ist in [roles] nicht definiert "
            f"(verfügbar: {wc['valid_roles']}).")
    task = {
        "role": role,
        "prompt": "Diagnose-Task (Spawn-Vektor, kein echter Lauf)",
        "output_path": f"output/diag-{role}",
    }
    out = wc["workspace"] / "output" / f"diag-{role}"
    return build_pi_cmd(wc, task, wc["worker_provider"], wc["default_model"], out)


def cmd_watchdog(wc: dict, once: bool) -> int:
    ensure_dirs(wc)
    check_running_stale(wc)
    boot_reconcile(wc)

    print(f"⏱️  Watchdog started | poll={wc['poll_interval_sec']}s | "
          f"default-model={wc['default_model']} | workspace={wc['workspace']}",
          flush=True)
    print(f"   models: {', '.join(wc['worker_models'])} | "
          f"cloud={wc['cloud_provider']}/{wc['cloud_default_model']}", flush=True)
    print(f"   roles: {', '.join(wc['valid_roles'])}", flush=True)

    if once:
        task = claim_next(wc)
        if task:
            run_task(wc, task)
        else:
            print("   Keine pending Tasks", flush=True)
        return 0

    while True:
        task = claim_next(wc)
        if task:
            run_task(wc, task)
        time.sleep(wc["poll_interval_sec"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Direkter Watchdog-Einstieg (`python3 worker.py [--once] [--home H]`).

    Normaler Regelbetrieb läuft über `pib watchdog`; dieser Einstieg dient
    der Direkt-Diagnose und dem Schreiben-Zugriff des systemd-Service.
    """
    parser = argparse.ArgumentParser(prog="worker", description="pi-bundle Worker")
    parser.add_argument("--once", action="store_true",
                        help="Nur einen Task, dann Exit")
    parser.add_argument("--home", default=None,
                        help="Bundle-Root überschreiben (Env PI_BUNDLE_HOME sonst)")
    parser.add_argument("--show-cmd", default=None, metavar="ROLE",
                        help="Nur den pi-Spawn-Vektor für ROLE ausgeben "
                             "(Default-Pfade/Override/Fehlverhalten), kein Spawn")
    args = parser.parse_args(argv)

    try:
        wc = load_worker_config(args.home)
    except ConfigError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2
    if args.show_cmd:
        try:
            print("CMD:", " ".join(spawn_vector(wc, args.show_cmd)))
        except ConfigError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 2
        return 0
    return cmd_watchdog(wc, args.once)


if __name__ == "__main__":
    sys.exit(main())
