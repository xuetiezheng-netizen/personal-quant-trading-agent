"""Local, single-user web console for the safe research workflows.

The web console deliberately stays small and uses only the Python standard
library.  It does not expose a shell, a file browser, a broker, or a scheduler:
the few buttons on the page map to the repository's existing PowerShell
launchers.  Bind to localhost only; this is a desktop helper, not a network
service.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

APP_ID = "personal-quant-trading-agent-web"
HOST = "127.0.0.1"
PORT = 8765
MAX_REQUEST_BYTES = 16 * 1024
MAX_LOG_BYTES = 256 * 1024

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
DATA_DIR = REPO_ROOT / "data"
REPORTS_DIR = REPO_ROOT / "data" / "reports"
DEMO_REPORTS_DIR = REPO_ROOT / "data" / "demo_reports"
CODEX_REVIEWS_DIR = REPO_ROOT / "data" / "codex_reviews"

# These are the only workflow action names accepted by the HTTP API.  The
# values are argument tuples, not user-controlled shell fragments.
ACTION_COMMANDS: dict[str, tuple[str, ...]] = {
    "demo": ("run-demo.ps1",),
    "capture_close": ("run-real.ps1", "-Mode", "CaptureClose"),
    "research": ("run-real.ps1", "-Mode", "Research"),
}
ACTION_LABELS: dict[str, str] = {
    "demo": "运行演示",
    "capture_close": "收盘快照",
    "research": "生成真实研究",
    "codex_review": "Codex 解读最新真实报告",
}
FOLDER_PATHS: dict[str, Path] = {
    "data": DATA_DIR,
    "codex_reviews": CODEX_REVIEWS_DIR,
}
STATIC_FILES: dict[str, str] = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class WebAppError(RuntimeError):
    """An expected, user-facing local-console error."""


class TaskBusyError(WebAppError):
    """Raised when a second workflow is requested while one is running."""


@dataclass
class TaskState:
    """A JSON-friendly snapshot of one local workflow invocation."""

    task_id: str
    action: str
    label: str
    started_at: str
    status: str = "running"
    finished_at: str | None = None
    exit_code: int | None = None
    output: str = ""
    result_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action": self.action,
            "label": self.label,
            "started_at": self.started_at,
            "status": self.status,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "output": self.output,
            "result_path": self.result_path,
        }


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _latest_file(directory: Path, pattern: str) -> Path | None:
    """Return the newest regular file by mtime, with a stable name tie-break."""

    if not directory.is_dir():
        return None
    candidates = [path for path in directory.glob(pattern) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def latest_real_report(repo_root: Path = REPO_ROOT) -> Path | None:
    return _latest_file(repo_root / "data" / "reports", "real-report-*.md")


def latest_demo_report(repo_root: Path = REPO_ROOT) -> Path | None:
    return _latest_file(repo_root / "data" / "demo_reports", "demo-report-*.md")


def latest_codex_review(repo_root: Path = REPO_ROOT) -> Path | None:
    return _latest_file(repo_root / "data" / "codex_reviews", "codex-review-*.md")


def _latest_snapshot(repo_root: Path = REPO_ROOT) -> Path | None:
    return _latest_file(repo_root / "data" / "raw_snapshots", "sina-*.json")


def _safe_display_path(path: Path | None, repo_root: Path = REPO_ROOT) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return None


def _find_powershell() -> str:
    """Find PowerShell without accepting a command from the HTTP request."""

    for name in ("pwsh", "powershell"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    raise WebAppError("未找到 PowerShell，无法运行研究流程。请确认 Windows PowerShell 或 pwsh 已安装。")


def _bounded_output(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    if len(text.encode("utf-8")) <= MAX_LOG_BYTES:
        return text
    # Keep the tail: PowerShell normally prints the useful error at the end.
    encoded = text.encode("utf-8")[-MAX_LOG_BYTES:]
    return "[日志过长，已截取末尾]\n" + encoded.decode("utf-8", errors="replace")


def _script_command(repo_root: Path, action: str) -> list[str]:
    """Build a fixed PowerShell argv list for an allowed action."""

    if action not in ACTION_COMMANDS:
        raise WebAppError("不支持的操作。请从页面上的固定按钮中选择。")

    powershell = _find_powershell()
    script_and_args = ACTION_COMMANDS[action]
    script_path = repo_root / script_and_args[0]
    if not script_path.is_file():
        raise WebAppError(f"找不到工作流脚本：{script_path.name}")
    return [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        *script_and_args[1:],
    ]


def _codex_command(repo_root: Path, report_path: Path) -> list[str]:
    """Build the only permitted Codex invocation for the latest real report."""

    report = report_path.resolve()
    reports_root = (repo_root / "data" / "reports").resolve()
    try:
        report.relative_to(reports_root)
    except ValueError as exc:
        raise WebAppError("真实报告路径不在固定报告目录内，已拒绝调用 Codex。") from exc
    if not report.is_file() or report.name.startswith("real-report-") is False or report.suffix != ".md":
        raise WebAppError("未找到可供 Codex 解读的真实报告。")

    powershell = _find_powershell()
    script_path = repo_root / "run-codex-review.ps1"
    if not script_path.is_file():
        raise WebAppError("找不到 Codex 解读脚本：run-codex-review.ps1")
    return [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-ReportPath",
        str(report),
        "-Yes",
    ]


def command_for_action(action: str, repo_root: Path = REPO_ROOT) -> list[str]:
    """Public test seam: return the exact fixed argv for an allowed action."""

    return _script_command(repo_root, action)


@dataclass
class TaskManager:
    repo_root: Path = REPO_ROOT
    popen_factory: Callable[..., Any] = subprocess.Popen
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _current: TaskState | None = field(default=None, init=False, repr=False)
    _history: deque[TaskState] = field(default_factory=lambda: deque(maxlen=12), init=False, repr=False)

    def current(self) -> TaskState | None:
        with self._lock:
            return self._current

    def start(self, action: str, *, confirm_usage: bool = False) -> TaskState:
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise TaskBusyError("已有任务正在运行，请等待它完成后再试。")

            if action == "codex_review":
                if confirm_usage is not True:
                    raise WebAppError("调用 Codex 前必须确认：报告内容会发送给 ChatGPT/Codex，并消耗账户用量。")
                report = latest_real_report(self.repo_root)
                if report is None:
                    raise WebAppError("还没有真实研究报告，请先运行“生成真实研究”。")
                argv = _codex_command(self.repo_root, report)
            else:
                argv = _script_command(self.repo_root, action)

            task = TaskState(
                task_id=uuid.uuid4().hex,
                action=action,
                label=ACTION_LABELS.get(action, action),
                started_at=_now_text(),
            )
            self._current = task
            worker = threading.Thread(
                target=self._run,
                args=(task, argv),
                name=f"quant-web-{action}",
                daemon=True,
            )
            worker.start()
            return task

    def _run(self, task: TaskState, argv: list[str]) -> None:
        try:
            process = self.popen_factory(
                argv,
                cwd=str(self.repo_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
            )
            raw_output, _ = process.communicate()
            exit_code = int(process.returncode)
            output = _bounded_output(raw_output or b"")
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the UI
            exit_code = 1
            output = f"启动工作流失败：{exc}"

        with self._lock:
            task.exit_code = exit_code
            task.status = "succeeded" if exit_code == 0 else "failed"
            task.finished_at = _now_text()
            task.output = output
            task.result_path = _safe_display_path(self._result_for(task.action), self.repo_root)
            self._history.appendleft(task)

    def _result_for(self, action: str) -> Path | None:
        if action == "demo":
            return latest_demo_report(self.repo_root)
        if action == "capture_close":
            return _latest_snapshot(self.repo_root)
        if action == "research":
            return latest_real_report(self.repo_root)
        if action == "codex_review":
            return latest_codex_review(self.repo_root)
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            current = self._current.as_dict() if self._current is not None else None
            recent = [task.as_dict() for task in self._history]
        return {
            "current": current,
            "recent": recent,
            "paths": {
                "latest_demo_report": _safe_display_path(latest_demo_report(self.repo_root), self.repo_root),
                "latest_snapshot": _safe_display_path(_latest_snapshot(self.repo_root), self.repo_root),
                "latest_real_report": _safe_display_path(latest_real_report(self.repo_root), self.repo_root),
                "latest_codex_review": _safe_display_path(latest_codex_review(self.repo_root), self.repo_root),
            },
        }


class LocalWebApplication:
    """Application state kept on the HTTP server instance."""

    def __init__(self, repo_root: Path = REPO_ROOT, *, token: str | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.tasks = TaskManager(self.repo_root)
        self._shutdown_requested = threading.Event()

    def render_index(self) -> bytes:
        path = WEB_ROOT if self.repo_root == REPO_ROOT else self.repo_root / "web"
        index_path = path / "index.html"
        html = index_path.read_text(encoding="utf-8")
        return html.replace("__LOCAL_TOKEN_VALUE__", self.token).encode("utf-8")

    def status(self) -> dict[str, Any]:
        snapshot = self.tasks.snapshot()
        snapshot.update({"app_id": APP_ID, "server": {"host": HOST, "port": PORT}})
        return snapshot

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], app: LocalWebApplication) -> None:
        self.app = app
        super().__init__(server_address, LocalRequestHandler)


class LocalRequestHandler(BaseHTTPRequestHandler):
    server: LocalHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # The helper runs via pythonw.exe; do not print request data or any
        # inherited environment values into a terminal/log by default.
        return

    @property
    def app(self) -> LocalWebApplication:
        return self.server.app

    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict[str, Any] | None:
        header = self.headers.get("Content-Length")
        try:
            length = int(header or "0")
        except ValueError:
            self._error("请求体长度无效。", HTTPStatus.BAD_REQUEST)
            return None
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._error("请求体过大，已拒绝。", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error("请求体必须是 UTF-8 JSON。", HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(payload, dict):
            self._error("请求体必须是 JSON 对象。", HTTPStatus.BAD_REQUEST)
            return None
        return payload

    def _check_token(self) -> bool:
        supplied = self.headers.get("X-Local-Token", "")
        if not secrets.compare_digest(supplied, self.app.token):
            self._error("本地访问令牌无效。", HTTPStatus.FORBIDDEN)
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "app_id": APP_ID, "host": HOST, "port": PORT})
            return
        if parsed.path == "/api/status":
            self._send_json({"ok": True, **self.app.status()})
            return

        relative = unquote(parsed.path)
        filename = STATIC_FILES.get(relative)
        if filename is None:
            self._error("找不到页面。", HTTPStatus.NOT_FOUND)
            return
        root = self.app.repo_root / "web"
        path = (root / filename).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            self._error("非法静态路径。", HTTPStatus.NOT_FOUND)
            return
        try:
            if filename == "index.html":
                body = self.app.render_index()
            else:
                body = path.read_bytes()
        except OSError:
            self._error("页面文件不可读。", HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_bytes(body, CONTENT_TYPES[path.suffix])

    def do_POST(self) -> None:
        if not self._check_token():
            return
        parsed = urlsplit(self.path)
        payload = self._read_json()
        if payload is None:
            return

        if parsed.path in {"/api/action", "/api/actions"}:
            action = payload.get("action")
            if not isinstance(action, str) or action not in (*ACTION_COMMANDS, "codex_review"):
                self._error("不支持的操作。请从页面上的固定按钮中选择。", HTTPStatus.BAD_REQUEST)
                return
            try:
                task = self.app.tasks.start(action, confirm_usage=payload.get("confirm_usage") is True)
            except TaskBusyError as exc:
                self._error(str(exc), HTTPStatus.CONFLICT)
            except WebAppError as exc:
                self._error(str(exc), HTTPStatus.BAD_REQUEST)
            else:
                self._send_json({"ok": True, "task": task.as_dict()}, HTTPStatus.ACCEPTED)
            return

        if parsed.path == "/api/open-folder":
            folder = payload.get("folder")
            target = FOLDER_PATHS.get(folder) if isinstance(folder, str) else None
            if target is None:
                self._error("不支持的文件夹。", HTTPStatus.BAD_REQUEST)
                return
            target = (self.app.repo_root / "data" / target.relative_to(REPO_ROOT / "data")).resolve()
            data_root = (self.app.repo_root / "data").resolve()
            try:
                target.relative_to(data_root)
            except ValueError:
                self._error("非法文件夹路径。", HTTPStatus.BAD_REQUEST)
                return
            try:
                target.mkdir(parents=True, exist_ok=True)
                if hasattr(os, "startfile"):
                    os.startfile(str(target))  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["explorer.exe", str(target)], shell=False)
            except OSError as exc:
                self._error(f"打开文件夹失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json({"ok": True, "folder": folder})
            return

        if parsed.path == "/api/shutdown":
            if self.app.tasks.current() is not None and self.app.tasks.current().status == "running":
                self._error("当前任务仍在运行，请等待完成后再停止服务。", HTTPStatus.CONFLICT)
                return
            self._send_json({"ok": True, "message": "本地服务即将停止。"})
            self.app.request_shutdown()
            return

        self._error("找不到接口。", HTTPStatus.NOT_FOUND)


def create_server(
    host: str = HOST,
    port: int = PORT,
    *,
    repo_root: Path = REPO_ROOT,
    token: str | None = None,
) -> LocalHTTPServer:
    """Create a testable localhost server without starting its loop."""

    app = LocalWebApplication(repo_root.resolve(), token=token)
    return LocalHTTPServer((host, port), app)


def serve(host: str = HOST, port: int = PORT, *, repo_root: Path = REPO_ROOT) -> None:
    server = create_server(host, port, repo_root=repo_root)
    try:
        while not server.app._shutdown_requested.is_set():
            server.handle_request()
    finally:
        server.server_close()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
