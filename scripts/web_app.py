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
import re
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

from trading_agent.swing.portfolio import (
    PortfolioRevisionConflictError,
    PortfolioStorageError,
    PortfolioStore,
    PortfolioValidationError,
)

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
PRIVATE_DATA_DIR = REPO_ROOT / "data" / "private"
PRIVATE_HOLDINGS_PATH = PRIVATE_DATA_DIR / "holdings.json"
PRIVATE_RESULTS_PATH = PRIVATE_DATA_DIR / "latest-results.json"
PRIVATE_REPORTS_DIR = PRIVATE_DATA_DIR / "reports"

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
# Swing actions are deliberately a separate namespace from the existing demo and
# research actions.  The HTTP layer accepts only these three values and builds
# their argv itself; it never treats a browser value as a script or shell text.
SWING_ACTIONS = frozenset({"analyze", "backtest", "all"})
SWING_ACTION_LABELS: dict[str, str] = {
    "analyze": "分析持仓波段状态",
    "backtest": "回放持仓波段效果",
    "all": "分析并回放持仓波段",
}
_HOLDING_FIELDS = frozenset(
    {
        "code",
        "name",
        "asset_type",
        "quantity",
        "avg_cost_cny",
        "acquired_date",
        "note",
        "revision",
        "tactical_ratio",
    }
)
_HOLDING_MUTATION_FIELDS = _HOLDING_FIELDS - {"revision"}
_SWING_ACTION_FIELDS = frozenset({"action", "code", "asset_type"})
_CODE_PATTERN = r"^[0-9]{6}$"
_ASSET_TYPES = frozenset({"stock", "etf"})
FOLDER_RELATIVE_PATHS: dict[str, Path] = {
    "data": Path("data"),
    "codex_reviews": Path("data") / "codex_reviews",
    "private_reports": Path("data") / "private" / "reports",
}
FOLDER_PATHS: dict[str, Path] = {
    key: REPO_ROOT / relative_path for key, relative_path in FOLDER_RELATIVE_PATHS.items()
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
        # API paths are portable display values even though the service runs on
        # Windows; they are never accepted back as filesystem input.
        return str(path.resolve().relative_to(repo_root.resolve())).replace("\\", "/")
    except ValueError:
        return None


def latest_swing_results(repo_root: Path = REPO_ROOT) -> Path | None:
    """Return the one fixed private swing-result document, if it exists."""

    try:
        path = _fixed_private_path(repo_root, "data/private/latest-results.json")
    except WebAppError:
        return None
    return path if path.is_file() else None


def latest_swing_report(repo_root: Path = REPO_ROOT) -> Path | None:
    """Return the newest Markdown report from the fixed private report folder."""

    try:
        report_dir = _fixed_private_path(repo_root, "data/private/reports")
    except WebAppError:
        return None
    return _latest_file(report_dir, "*.md")


def _fixed_private_path(repo_root: Path, relative_path: str) -> Path:
    """Resolve a server-owned private path without accepting browser input."""

    root = repo_root.resolve()
    private_root = (root / "data" / "private").resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(private_root)
    except ValueError as exc:
        raise WebAppError("私有结果路径配置无效。") from exc
    return path


def _safe_result_value(value: Any, repo_root: Path, *, key: str = "") -> Any:
    """Copy JSON result data while hiding stacks and absolute filesystem paths.

    The result file is produced by a fixed local script, but it is still treated
    as untrusted display data.  In particular, a failed run must not turn an
    exception traceback or local path into an HTTP response.
    """

    lowered_key = key.casefold()
    if lowered_key in {
        "traceback",
        "stack",
        "stacktrace",
        "stack_trace",
        "exception",
        "stderr",
        "absolute_path",
        "absolutepath",
    }:
        return "[诊断信息已隐藏]"
    if isinstance(value, dict):
        return {
            str(item_key): _safe_result_value(item_value, repo_root, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_result_value(item, repo_root, key=key) for item in value]
    if isinstance(value, str):
        if "traceback (most recent call last)" in value.casefold():
            return "[诊断信息已隐藏]"
        # Keep relative report paths useful while ensuring absolute paths never
        # leave the service.  Both slash styles occur in Windows JSON output.
        repo_text = str(repo_root.resolve())
        for prefix in (repo_text, repo_text.replace("\\", "/")):
            if value.startswith(prefix):
                relative = _safe_display_path(Path(value), repo_root)
                return relative or "[本地路径已隐藏]"
            value = value.replace(prefix, "<项目目录>")
        if re.search(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"']+", value):
            return "[本地路径已隐藏]"
        return value
    return value


def read_swing_results(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Read only the fixed private result JSON and report directory.

    Returned paths are repository-relative.  This endpoint intentionally does
    not accept a path or filename query parameter.
    """

    latest_path = latest_swing_results(repo_root)
    result_payload: Any = None
    if latest_path is not None:
        try:
            result_payload = json.loads(latest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WebAppError("最新波段结果暂时无法读取。") from exc

    report_dir = _fixed_private_path(repo_root, "data/private/reports")
    report_paths: list[dict[str, str]] = []
    if report_dir.is_dir():
        reports = sorted(
            (path for path in report_dir.glob("*.md") if path.is_file()),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for path in reports:
            relative = _safe_display_path(path, repo_root)
            if relative is not None:
                report_paths.append({"path": relative, "name": path.name})

    latest_relative = _safe_display_path(latest_path, repo_root)
    latest_report_relative = report_paths[0]["path"] if report_paths else None
    safe_result = _safe_result_value(result_payload, repo_root)
    response: dict[str, Any] = {
        "latest_results_path": latest_relative,
        "latest_report_path": latest_report_relative,
        "reports": report_paths,
        "result": safe_result,
    }
    # Keep the useful, stable fields available at the top level for the browser
    # while retaining ``result`` as an explicit namespaced copy for callers that
    # prefer not to depend on the JSON document shape.
    if isinstance(safe_result, dict):
        for key in ("schema_version", "updated_at", "results", "mode"):
            if key in safe_result:
                response[key] = safe_result[key]
    return response


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


def _safe_task_output(raw: bytes | str, repo_root: Path) -> str:
    """Keep task logs useful without returning local paths or tracebacks."""

    text = _bounded_output(raw)
    if "traceback (most recent call last)" in text.casefold():
        return "任务失败；详细诊断堆栈未在浏览器中展示。"
    root_text = str(repo_root.resolve())
    for prefix in (root_text, root_text.replace("\\", "/")):
        text = text.replace(prefix, "<项目目录>")
    return re.sub(r"(?i)(?<!https:)(?<!http:)(?:[A-Za-z]:[\\/][^\s\r\n]+)", "<本地路径已隐藏>", text)


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


def _validate_swing_selector(code: object = None, asset_type: object = None) -> tuple[str | None, str | None]:
    """Validate the optional selector used by the fixed swing command."""

    if (code is None) != (asset_type is None):
        raise WebAppError("指定波段标的时必须同时提供 code 和 asset_type。")
    if code is None and asset_type is None:
        return None, None
    if not isinstance(code, str) or re.fullmatch(_CODE_PATTERN, code) is None:
        raise WebAppError("code 必须是6位数字。")
    if not isinstance(asset_type, str) or asset_type not in _ASSET_TYPES:
        raise WebAppError("asset_type 只能是 stock 或 etf。")
    return code, asset_type


def _swing_command(
    repo_root: Path,
    action: str,
    *,
    code: object = None,
    asset_type: object = None,
) -> list[str]:
    """Build the only permitted argv for the local swing runner.

    The executable and script are repository-owned fixed paths.  ``shell=False``
    is enforced by :class:`TaskManager`; this function only returns argv data and
    never concatenates a browser value into a command string.
    """

    if action not in SWING_ACTIONS:
        raise WebAppError("不支持的波段操作。请从固定按钮中选择。")
    normalized_code, normalized_asset_type = _validate_swing_selector(code, asset_type)
    root = repo_root.resolve()
    python_path = root / ".venv" / "Scripts" / "python.exe"
    script_path = root / "scripts" / "run_swing.py"
    if not python_path.is_file():
        raise WebAppError("找不到项目虚拟环境 Python，无法运行波段流程。")
    if not script_path.is_file():
        raise WebAppError("找不到波段流程脚本：run_swing.py")
    # ``run_swing.py`` declares the mode as its only positional argument and
    # accepts a fixed ``--code`` selector.  ``asset_type`` is validated at the
    # HTTP boundary for an unambiguous request, while the runner resolves the
    # actual type from the private holding record.
    argv = [str(python_path), "-X", "utf8", str(script_path), action]
    if normalized_code is not None and normalized_asset_type is not None:
        argv.extend(("--code", normalized_code, "--asset-type", normalized_asset_type))
    return argv


def command_for_swing_action(
    action: str,
    repo_root: Path = REPO_ROOT,
    *,
    code: object = None,
    asset_type: object = None,
) -> list[str]:
    """Public test seam for the fixed swing runner argv."""

    return _swing_command(repo_root, action, code=code, asset_type=asset_type)


# Backwards-friendly descriptive alias for tests or local callers that prefer
# the verb before the noun.
swing_command_for_action = command_for_swing_action


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

    def start(
        self,
        action: str,
        *,
        confirm_usage: bool = False,
        code: object = None,
        asset_type: object = None,
    ) -> TaskState:
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise TaskBusyError("已有任务正在运行，请等待它完成后再试。")

            if action in SWING_ACTIONS:
                # The command builder validates the selector and appends only
                # the two fixed option names understood by run_swing.py.
                argv = _swing_command(
                    self.repo_root,
                    action,
                    code=code,
                    asset_type=asset_type,
                )
            elif action == "codex_review":
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
                label=SWING_ACTION_LABELS.get(action, ACTION_LABELS.get(action, action)),
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
            output = _safe_task_output(raw_output or b"", self.repo_root)
        except Exception:  # noqa: BLE001 - surface worker failures in the UI
            exit_code = 1
            output = "启动工作流失败；详细诊断未在浏览器中展示。"

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
        if action in SWING_ACTIONS:
            return latest_swing_results(self.repo_root) or latest_swing_report(self.repo_root)
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
                "latest_swing_results": _safe_display_path(
                    latest_swing_results(self.repo_root), self.repo_root
                ),
                "latest_swing_report": _safe_display_path(
                    latest_swing_report(self.repo_root), self.repo_root
                ),
            },
        }


class LocalWebApplication:
    """Application state kept on the HTTP server instance."""

    def __init__(self, repo_root: Path = REPO_ROOT, *, token: str | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.tasks = TaskManager(self.repo_root)
        # The browser can use this store only through authenticated routes. Its
        # path is derived solely from the server repository root; no request
        # field is ever treated as a filesystem path.
        self.holdings_store = PortfolioStore(self.repo_root / "data" / "private" / "holdings.json")
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

    @staticmethod
    def _holding_route(path: str) -> tuple[str, str] | None:
        """Strictly parse the two path parameters for a holding endpoint."""

        decoded = unquote(path)
        parts = decoded.split("/")
        if len(parts) != 5 or parts[0:3] != ["", "api", "holdings"]:
            return None
        asset_type, code = parts[3], parts[4]
        if asset_type not in _ASSET_TYPES or re.fullmatch(_CODE_PATTERN, code) is None:
            return None
        return asset_type, code

    def _portfolio_error(self, exc: Exception) -> None:
        if isinstance(exc, PortfolioRevisionConflictError):
            self._error("持仓数据已被更新，请刷新后重试。", HTTPStatus.CONFLICT)
        elif isinstance(exc, PortfolioValidationError):
            self._error(str(exc), HTTPStatus.BAD_REQUEST)
        elif isinstance(exc, PortfolioStorageError):
            self._error("本机私有持仓暂时无法读取或保存。", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            # Do not turn an implementation traceback or filesystem path into
            # a response body.
            self._error("本机私有持仓操作失败。", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _validate_payload_fields(self, payload: dict[str, Any], allowed: frozenset[str]) -> bool:
        if set(payload) - allowed:
            self._error("请求包含不支持的字段。", HTTPStatus.BAD_REQUEST)
            return False
        return True

    def _expected_revision(self, payload: dict[str, Any], *, required: bool) -> int | None:
        if "expected_revision" not in payload:
            if required:
                self._error("expected_revision 必须是非负整数。", HTTPStatus.BAD_REQUEST)
            return None
        value = payload["expected_revision"]
        if type(value) is not int or value < 0:
            self._error("expected_revision 必须是非负整数。", HTTPStatus.BAD_REQUEST)
            return None
        return value

    def _send_snapshot(self, snapshot: Any, status: int = HTTPStatus.OK) -> None:
        self._send_json({"ok": True, **snapshot.as_dict()}, status)

    def _handle_holdings_get(self) -> None:
        try:
            self._send_snapshot(self.app.holdings_store.load())
        except Exception as exc:  # noqa: BLE001 - sanitize all private-store failures
            self._portfolio_error(exc)

    def _handle_holdings_post(self, payload: dict[str, Any]) -> None:
        allowed = _HOLDING_MUTATION_FIELDS | {"expected_revision"}
        if not self._validate_payload_fields(payload, allowed):
            return
        expected_revision = self._expected_revision(payload, required=False)
        if "expected_revision" in payload and expected_revision is None:
            return
        record = {key: value for key, value in payload.items() if key != "expected_revision"}
        try:
            snapshot = self.app.holdings_store.add_holding(
                record, expected_revision=expected_revision
            )
        except Exception as exc:  # noqa: BLE001 - sanitize all private-store failures
            self._portfolio_error(exc)
        else:
            self._send_snapshot(snapshot, HTTPStatus.CREATED)

    def _handle_holdings_put(self, parsed_path: str, payload: dict[str, Any]) -> None:
        route = self._holding_route(parsed_path)
        if route is None:
            self._error("持仓接口路径无效。", HTTPStatus.NOT_FOUND)
            return
        if not self._validate_payload_fields(payload, _HOLDING_MUTATION_FIELDS | {"expected_revision"}):
            return
        expected_revision = self._expected_revision(payload, required=True)
        if expected_revision is None:
            return
        asset_type, code = route
        for identity_field, route_value in (("code", code), ("asset_type", asset_type)):
            if identity_field in payload and payload[identity_field] != route_value:
                self._error(f"{identity_field} 与路径不一致。", HTTPStatus.BAD_REQUEST)
                return
        changes = {
            key: value
            for key, value in payload.items()
            if key not in {"expected_revision", "code", "asset_type"}
        }
        try:
            snapshot = self.app.holdings_store.update_holding(
                code,
                changes,
                asset_type=asset_type,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # noqa: BLE001 - sanitize all private-store failures
            self._portfolio_error(exc)
        else:
            self._send_snapshot(snapshot)

    def _handle_holdings_delete(self, parsed_path: str, payload: dict[str, Any]) -> None:
        route = self._holding_route(parsed_path)
        if route is None:
            self._error("持仓接口路径无效。", HTTPStatus.NOT_FOUND)
            return
        if set(payload) != {"expected_revision"}:
            self._error("删除持仓只接受 expected_revision。", HTTPStatus.BAD_REQUEST)
            return
        expected_revision = self._expected_revision(payload, required=True)
        if expected_revision is None:
            return
        asset_type, code = route
        try:
            snapshot = self.app.holdings_store.delete_holding(
                code,
                asset_type=asset_type,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # noqa: BLE001 - sanitize all private-store failures
            self._portfolio_error(exc)
        else:
            self._send_snapshot(snapshot)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "app_id": APP_ID, "host": HOST, "port": PORT})
            return
        if parsed.path in {"/api/status", "/api/holdings", "/api/swing-results"} and not self._check_token():
            return
        if parsed.path == "/api/status":
            self._send_json({"ok": True, **self.app.status()})
            return
        if parsed.path == "/api/holdings":
            self._handle_holdings_get()
            return
        if parsed.path == "/api/swing-results":
            try:
                self._send_json({"ok": True, **read_swing_results(self.app.repo_root)})
            except WebAppError as exc:
                self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
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

    def do_PUT(self) -> None:
        if not self._check_token():
            return
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/holdings/"):
            self._error("找不到接口。", HTTPStatus.NOT_FOUND)
            return
        payload = self._read_json()
        if payload is None:
            return
        self._handle_holdings_put(parsed.path, payload)

    def do_DELETE(self) -> None:
        if not self._check_token():
            return
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/holdings/"):
            self._error("找不到接口。", HTTPStatus.NOT_FOUND)
            return
        payload = self._read_json()
        if payload is None:
            return
        self._handle_holdings_delete(parsed.path, payload)

    def do_POST(self) -> None:
        if not self._check_token():
            return
        parsed = urlsplit(self.path)
        payload = self._read_json()
        if payload is None:
            return

        if parsed.path == "/api/holdings":
            self._handle_holdings_post(payload)
            return

        if parsed.path == "/api/swing-action":
            if not self._validate_payload_fields(payload, _SWING_ACTION_FIELDS):
                return
            action = payload.get("action")
            if not isinstance(action, str) or action not in SWING_ACTIONS:
                self._error("不支持的波段操作。请从固定按钮中选择。", HTTPStatus.BAD_REQUEST)
                return
            try:
                selector_supplied = "code" in payload or "asset_type" in payload
                if selector_supplied and (
                    "code" not in payload
                    or "asset_type" not in payload
                    or payload.get("code") is None
                    or payload.get("asset_type") is None
                ):
                    raise WebAppError("指定波段标的时必须同时提供有效的 code 和 asset_type。")
                code, asset_type = _validate_swing_selector(
                    payload.get("code"), payload.get("asset_type")
                )
                task = self.app.tasks.start(
                    action,
                    code=code,
                    asset_type=asset_type,
                )
            except TaskBusyError as exc:
                self._error(str(exc), HTTPStatus.CONFLICT)
            except WebAppError as exc:
                self._error(str(exc), HTTPStatus.BAD_REQUEST)
            else:
                self._send_json({"ok": True, "task": task.as_dict()}, HTTPStatus.ACCEPTED)
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
            relative = FOLDER_RELATIVE_PATHS.get(folder) if isinstance(folder, str) else None
            if relative is None:
                self._error("不支持的文件夹。", HTTPStatus.BAD_REQUEST)
                return
            target = (self.app.repo_root / relative).resolve()
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
