# Zusätzliche Werkzeuge in diesem Fork

Drei Ergänzungen zu claude-swap, entstanden beim Einrichten von zwei Konten
(privat und work) auf einer Windows-Maschine.

## Die zentrale Erkenntnis vorweg

**CLI und Desktop-App verwalten ihr Konto völlig unterschiedlich.**

| | Quelle der Wahrheit | Umschaltbar durch cswap |
|---|---|---|
| Claude Code CLI / VS Code | `~/.claude/.credentials.json` | ja |
| Claude Desktop-App | Web-Session (Cookies + LocalStorage) | nein |

Der Token-Cache der Desktop-App in `%APPDATA%\Claude\config.json` sieht aus wie
ein Anmeldespeicher, ist aber nur ein Cache. Gemessen am 27.07.2026: nach einem
Schreibzugriff von außen protokollierte die App beim nächsten Start
`clearing token cache`, ermittelte ihre Identität aus der Web-Session, meldete
`no cached token found` für genau den geschriebenen Schlüssel und holte sich
frische Tokens. Deshalb ist das Upstream-Issue
[#14](https://github.com/realiti4/claude-swap/issues/14) offen.

Für die Desktop-App hilft nur **ein eigenes Profil je Konto**.

## `chat_ui/` — Chat-Oberfläche mit Kontowechsel

Lokale Weboberfläche, die `claude -p --output-format stream-json` als
Kindprozess betreibt und die Ausgabe per Server-Sent-Events streamt. Die
Kontoliste, das Live-Kontingent und der Wechsel kommen von `cswap`.

```bash
uv run python scripts/chat_ui/server.py --cwd C:\pfad\zum\projekt
```

Öffnet auf <http://127.0.0.1:8765>, bindet ausschließlich an `127.0.0.1`, nur
Standardbibliothek. Der Wechsel wirkt ab der nächsten Nachricht; das Gespräch
bleibt erhalten (nachgewiesen: Turn 1 auf work, Wechsel, Turn 2 auf privat
erinnerte sich an Turn 1).

Werkzeuge sind standardmäßig abgeschaltet, weil im Headless-Betrieb niemand
Berechtigungsdialoge beantworten kann. Die Einstellung „Lesend" gibt `Read`,
`Glob`, `Grep` und Websuche frei.

## `desktop_profiles.py` — ein Profil je Konto für die Desktop-App

Jedes mit `--user-data-dir` gestartete Electron-Profil hat einen eigenen
Cookie-Jar, eigenen LocalStorage **und** einen eigenen OSCrypt-Schlüssel. Damit
laufen zwei Konten unabhängig nebeneinander.

```bash
python scripts/desktop_profiles.py list        # Profile mit Plan und Status
python scripts/desktop_profiles.py create work # Profil anlegen (ohne Login)
python scripts/desktop_profiles.py launch work # starten, dort anmelden
python scripts/desktop_profiles.py usage       # 5h/7d je Profil, live von der API
python scripts/desktop_profiles.py shortcuts   # Desktop-Verknüpfungen
```

`create` übernimmt MCP-Konfiguration und Oberflächen-Einstellungen aus dem
Standardprofil, aber **nicht** dessen Anmeldung.

Claude Codes eigener Zustand — Einstellungen, Projekte, Skills in `~/.claude` —
wird von allen Profilen geteilt, weil er am Home-Verzeichnis hängt und nicht am
Electron-Profil. Nur `claude_desktop_config.json` und `claude-code-sessions`
sind profilgebunden.

> Diese Befehle schreiben unter `%APPDATA%`. In einer Umgebung mit
> Dateisystem-Sandbox musst du sie in deiner eigenen Shell ausführen.

## `desktop_e2e.py` — Prüfstand für den Desktop-Speicher

Das Werkzeug, mit dem die Erkenntnis oben entstand. Liest den Anmeldespeicher
der Desktop-App, sichert ihn entschlüsselt und mit DPAPI neu verpackt, und kann
ihn zurückschreiben.

```bash
python scripts/desktop_e2e.py status
python scripts/desktop_e2e.py snapshot A
python scripts/desktop_e2e.py activate A   # App muss geschlossen sein
```

Nützlich zum Nachvollziehen oder wenn ein künftiger Build den Cache doch
respektiert. **Ein Kontowechsel wird damit nicht erreicht** — siehe oben.

Die Speicherschicht selbst liegt in
[`src/claude_swap/desktop_store.py`](../src/claude_swap/desktop_store.py) mit
Tests in [`tests/test_desktop_store.py`](../tests/test_desktop_store.py).
Windows only: macOS leitet denselben `v10`-Schlüssel per PBKDF2 aus einem
Keychain-Geheimnis ab, Linux nutzt ein festes Passwort oder den Desktop-Keyring.
