/**
 * pi-bundle-hook — Session-Start-Injektion + Session-Ende-Trigger
 *
 * 1) Session-Start: Bei `session_start` wird die INDEX.md (Spiegel des
 *    Bestands: Topics-Tabelle + Domänen-Sektionen) als custom_message in den
 *    Kontext injiziert — ohne triggerTurn (kein Modellaufruf, die Message ist
 *    ab der ersten Anfrage im Kontext). Geltungsbereich: nur TUI (interaktive
 *    Sessions); Gateway-Chat/RPC-Sessions laden den Index on-demand
 *    (`pib topic read`; 1:1-Gateway manuell). Idempotent: der Branch enthält
 *    bereits eine Injektion → skip (keine Doppel-Injektion bei Resume).
 *
 * 2) Session-Ende: Bei `session_shutdown` (quit/new/resume/fork — nicht
 *    reload) spawnt er `pib digest run --session <file>` DETACHED (überlebt
 *    das pi-Beenden). Der Digest-Bau filtert reload zusätzlich (Defense in
 *    depth) und reiht die curator-Tasks je [dispatch]-Zeile ein. Ob die
 *    Verarbeitung aktiv ist, entscheidet [trigger].session_ende in der
 *    config.toml (aus → pib digest run meldet laut No-op).
 *
 * Registriert von install.sh unter ~/.pi/agent/extensions/pi-bundle.ts —
 * pi lädt dort nur TypeScript/JavaScript-Module (docs/extensions.md); die
 * Bash-Vorlagen im Repo (hooks/session-start, hooks/session-end) bleiben
 * manuelle Schnittstelle (Tests/Dev) und werden nicht registriert.
 *
 * Overrides für Tests/Dev (Umgebungsvariablen):
 *   PI_BUNDLE_HOME  Instanz-Root (Default: ~/.pi/pi-bundle)
 *   PI_BUNDLE_INDEX Pfad zur INDEX.md (Default: <root>/memory/INDEX.md)
 *   PIB_BIN         pib-Aufruf (Default: python3 + <root>/lib/pib.py)
 *   PIB_PYTHON      Python-Binary (Default: python3)
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const INDEX_CUSTOM_TYPE = "pi-bundle-index";

function bundleRoot(): string {
  return process.env.PI_BUNDLE_HOME ?? join(homedir(), ".pi", "pi-bundle");
}

function resolveIndex(): string {
  return process.env.PI_BUNDLE_INDEX ?? join(bundleRoot(), "memory", "INDEX.md");
}

/** pib-Aufruf: $PIB_BIN > python3 + <root>/lib/pib.py (vom Installer immer abgelegt). */
function resolvePib(): { cmd: string; args: string[] } {
  const bin = process.env.PIB_BIN;
  if (bin) return { cmd: bin, args: [] };
  const python = process.env.PIB_PYTHON ?? "python3";
  return { cmd: python, args: [join(bundleRoot(), "lib", "pib.py")] };
}

export default function (pi: ExtensionAPI) {
  // ---- Session-Start-Injektion -------------------------------------------
  pi.on("session_start", async (event, ctx) => {
    // reload = dieselbe Session wird weitergeführt (Extension-Reload) — kein Re-Injekt.
    if (event.reason === "reload") return;
    // Nur TUI (interaktive Sessions); Gateway-Chat/RPC-Sessions laden den
    // Index on-demand — keine erzwungene Injektion in Fremd-Modi.
    if (ctx.mode !== "tui") return;
    // Idempotenz: Branch enthält bereits eine Injektion → skip (keine
    // Doppel-Injektion bei häufigem Resume).
    const branch = ctx.sessionManager.getBranch();
    if (branch.some((e) =>
      e.type === "custom_message" && e.customType === INDEX_CUSTOM_TYPE,
    )) return;

    let index: string;
    try {
      index = readFileSync(resolveIndex(), "utf-8").trim();
    } catch (err) {
      // Kein stiller Zustand — laut auf dem Terminal, Session wird nicht blockiert.
      console.error("[pi-bundle-hook] INDEX.md nicht lesbar:", err);
      return;
    }
    if (!index) return;

    // Ohne triggerTurn: kein Modellaufruf — die Message ist ab der ersten
    // Anfrage im Kontext. display: false → unsichtbar, sauberes Transkript.
    pi.sendMessage({
      customType: INDEX_CUSTOM_TYPE,
      content: `## TOPIC-INDEX — dynamisch injiziert (Session-Start)\n\n${index}`,
      display: false,
    });
  });

  // ---- Session-Ende-Trigger ----------------------------------------------
  pi.on("session_shutdown", async (event, ctx) => {
    // reload = dieselbe Session wird weitergeführt (Protokoll offen) — kein Digest.
    if (event.reason === "reload") return;

    // Ephemere Sessions (--no-session) haben keine Datei — nichts zu verdichten.
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile || !existsSync(sessionFile)) return;

    const pib = resolvePib();
    // Detached (stdio ignore + unref): pi kann sofort beenden (quit-Fall), das
    // Kind (Digest → curator-Tasks) läuft weiter und loggt selbst.
    // [trigger].session_ende=aus → pib digest run meldet laut No-op.
    // Fehlgeschlagene Läufe holt der Nacht-Sweep nach (Erhaltungsgarantie).
    const child = spawn(
      pib.cmd,
      [...pib.args, "digest", "run", "--session", sessionFile],
      { detached: true, stdio: "ignore" },
    );
    child.on("error", (err) => {
      // Nur auf dem Pi-Terminal sichtbar; der Nacht-Sweep (Erhaltungsgarantie)
      // holt die Session später nach — hier ist kein Recovery möglich.
      console.error("[pi-bundle-hook] digest-Spawn fehlgeschlagen:", err);
    });
    child.unref();
  });
}
