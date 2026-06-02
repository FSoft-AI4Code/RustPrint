import json
import os
import queue
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rustprint.cli.config_manager import ConfigManager, deep_merge_defaults
from rustprint.web.runner import MigrationRunner

STATIC_DIR = Path(__file__).parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


_EDITOR_CLIS = (
    "cursor",
    "code",
    "code-insiders",
    "codium",
    "vscodium",
    "windsurf",
    "zed",
    "subl",
)

_MACOS_EDITOR_APPS = (
    ("Cursor", "Contents/Resources/app/bin/cursor"),
    ("Visual Studio Code", "Contents/Resources/app/bin/code"),
    ("Visual Studio Code - Insiders", "Contents/Resources/app/bin/code-insiders"),
    ("VSCodium", "Contents/Resources/app/bin/codium"),
    ("Windsurf", "Contents/Resources/app/bin/windsurf"),
    ("Zed", "Contents/MacOS/cli"),
    ("Sublime Text", "Contents/SharedSupport/bin/subl"),
)

_MACOS_APP_DIRS = ("/Applications", "~/Applications", "/System/Applications")


_WINDOWS_EDITOR_EXES = (
    r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe",
    r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
    r"%ProgramFiles%\Microsoft VS Code\Code.exe",
    r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
    r"%LOCALAPPDATA%\Programs\Microsoft VS Code Insiders\Code - Insiders.exe",
    r"%LOCALAPPDATA%\Programs\VSCodium\VSCodium.exe",
    r"%ProgramFiles%\VSCodium\VSCodium.exe",
    r"%LOCALAPPDATA%\Programs\Windsurf\Windsurf.exe",
    r"%ProgramFiles%\Zed\Zed.exe",
    r"%ProgramFiles%\Sublime Text\sublime_text.exe",
)

_LINUX_EDITOR_PATHS = (
    "/snap/bin/cursor",
    "/snap/bin/code",
    "/snap/bin/code-insiders",
    "/snap/bin/codium",
    "/snap/bin/zed",
    "/usr/share/code/bin/code",
    "/usr/bin/code",
    "/usr/local/bin/code",
    "/opt/cursor/cursor",
    "/usr/bin/zed",
)

_LINUX_FLATPAK_APPS = (
    "com.visualstudio.code",
    "com.vscodium.codium",
    "dev.zed.Zed",
)


def _macos_app_bundle(app_name: str) -> Path | None:
    for base in _MACOS_APP_DIRS:
        candidate = Path(base).expanduser() / f"{app_name}.app"
        if candidate.exists():
            return candidate
    return None


def _windows_editor_commands(target: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for raw in _WINDOWS_EDITOR_EXES:
        expanded = os.path.expandvars(raw)
        if "%" in expanded:
            continue
        exe = Path(expanded)
        if exe.exists():
            commands.append(["cmd", "/c", "start", "", str(exe), target])
    return commands


def _linux_editor_commands(target: str) -> list[list[str]]:
    commands: list[list[str]] = []
    seen: set[str] = set()
    for raw in _LINUX_EDITOR_PATHS:
        exe = Path(raw)
        if str(exe) in seen or not exe.exists():
            continue
        seen.add(str(exe))
        commands.append([str(exe), target])
    flatpak = shutil.which("flatpak")
    if flatpak:
        for app_id in _LINUX_FLATPAK_APPS:
            commands.append([flatpak, "run", app_id, target])
    return commands


def _verified_editor_commands(path: Path) -> list[list[str]]:
    target = str(path)
    commands: list[list[str]] = []
    for cli in _EDITOR_CLIS:
        found = shutil.which(cli)
        if found:
            commands.append([found, target])
    if sys.platform == "darwin":
        for app_name, cli_rel in _MACOS_EDITOR_APPS:
            bundle = _macos_app_bundle(app_name)
            if bundle is not None:
                bundled_cli = bundle / cli_rel
                if bundled_cli.exists():
                    commands.append([str(bundled_cli), target])
                commands.append(["open", "-a", str(bundle), target])
            else:
                commands.append(["open", "-a", app_name, target])
    elif sys.platform.startswith("win"):
        commands.extend(_windows_editor_commands(target))
    else:
        commands.extend(_linux_editor_commands(target))
    return commands


def _fallback_open_commands(path: Path) -> list[list[str]]:
    target = str(path)
    commands: list[list[str]] = []
    if sys.platform.startswith("win"):
        commands.append(["explorer", target])
    else:
        for opener in ("xdg-open", "gio"):
            found = shutil.which(opener)
            if found:
                commands.append([found, "open", target] if opener == "gio" else [found, target])
    return commands


def _open_in_workspace(target: str) -> tuple[bool, str]:
    if not target:
        return False, "No path provided"
    path = Path(target).expanduser()
    if not path.exists():
        return False, f"Path does not exist yet: {path}"
    for command in _verified_editor_commands(path):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=8)
        except (FileNotFoundError, OSError):
            continue
        except subprocess.TimeoutExpired:
            return True, f"Opening {path}"
        except Exception as exc:
            return False, str(exc)
        if result.returncode == 0:
            return True, f"Opening {path}"
    for command in _fallback_open_commands(path):
        try:
            subprocess.Popen(command)
        except (FileNotFoundError, OSError):
            continue
        except Exception as exc:
            return False, str(exc)
        return True, f"Opening {path}"
    return False, "No editor found (install an editor CLI such as Cursor or VS Code's 'code')"


def _config_payload() -> dict:
    try:
        manager = ConfigManager()
        if manager.config_exists():
            return {
                "config": manager.load_project_config(),
                "exists": True,
                "config_path": str(manager.config_file_path),
            }
    except Exception:
        pass
    data = deep_merge_defaults({})
    cwd = Path.cwd()
    data["source"]["path"] = str(cwd)
    data["output"]["base_dir"] = f"~/rustprint-output/{cwd.name}"
    data["output"]["cache"] = f"~/.cache/rustprint/{cwd.name}"
    return {"config": data, "exists": False, "config_path": None}


class _Handler(BaseHTTPRequestHandler):
    runner: MigrationRunner = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str) -> None:
        path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in path.parents or not path.is_file():
            self._send_json({"status": "error", "message": "Not found"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_static("index.html")
        elif self.path.startswith("/static/"):
            self._send_static(self.path[len("/static/"):])
        elif self.path == "/api/config":
            self._send_json(_config_payload())
        elif self.path == "/api/state":
            self._send_json({**self.runner.snapshot(), "logs": self.runner.bus.recent_logs()[-300:]})
        elif self.path == "/api/events":
            self._serve_events()
        else:
            self._send_json({"status": "error", "message": "Not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json({"status": "error", "message": "Invalid JSON body"}, 400)
            return

        if self.path == "/api/start":
            config = body.get("config", body)
            ok, message = self.runner.start(config)
            self._send_json({"status": "success" if ok else "error", "message": message}, 200 if ok else 409)
        elif self.path == "/api/stop":
            ok = self.runner.stop()
            self._send_json(
                {
                    "status": "success" if ok else "error",
                    "message": "Stopping after current stage" if ok else "No migration in progress",
                }
            )
        elif self.path == "/api/open":
            ok, message = _open_in_workspace(body.get("path", ""))
            self._send_json({"status": "success" if ok else "error", "message": message}, 200 if ok else 400)
        else:
            self._send_json({"status": "error", "message": "Not found"}, 404)

    def _serve_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = self.runner.bus.subscribe()
        try:
            snapshot = self.runner.bus.snapshot_event() or {"type": "state", **self.runner.snapshot()}
            self._sse_send(snapshot)
            for event in self.runner.bus.recent_logs():
                self._sse_send(event)
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_send(event)
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            self.runner.bus.unsubscribe(q)

    def _sse_send(self, event: dict) -> None:
        payload = "data: " + json.dumps(event) + "\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()


def create_server(host: str = "127.0.0.1", port: int = 5000, runner: MigrationRunner | None = None) -> ThreadingHTTPServer:
    runner = runner or MigrationRunner()
    handler = type("BoundHandler", (_Handler,), {"runner": runner})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def run_server(host: str = "127.0.0.1", port: int = 5000) -> None:
    httpd = create_server(host, port)
    banner = "RustPrint Web UI"
    print("=" * 80)
    print(banner.center(80))
    print("=" * 80)
    print(f"Serving at http://{host}:{port}")
    print("Press Ctrl+C to stop the server")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()
