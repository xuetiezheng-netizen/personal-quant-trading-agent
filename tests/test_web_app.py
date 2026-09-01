from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scripts import web_app

REPO_ROOT = Path(__file__).resolve().parents[1]


class RunningServer:
    def __init__(self, repo_root: Path = REPO_ROOT) -> None:
        self.server = web_app.create_server("127.0.0.1", 0, repo_root=repo_root, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def request(self, method: str, path: str, *, body: dict | None = None, token: str = "test-token"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"X-Local-Token": token}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw.decode("utf-8")) if raw else {}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


@pytest.fixture
def running_server():
    instance = RunningServer()
    try:
        yield instance
    finally:
        instance.close()


def test_health_has_fixed_app_id_and_localhost_defaults() -> None:
    assert web_app.HOST == "127.0.0.1"
    assert web_app.PORT == 8765
    server = web_app.create_server("127.0.0.1", 0, repo_root=REPO_ROOT, token="test-token")
    try:
        assert server.app.status()["app_id"] == web_app.APP_ID
    finally:
        server.server_close()


def test_home_embeds_random_local_token(running_server: RunningServer) -> None:
    status, payload = running_server.request("GET", "/api/health")
    assert status == 200
    assert payload["app_id"] == web_app.APP_ID

    connection = http.client.HTTPConnection("127.0.0.1", running_server.port, timeout=3)
    connection.request("GET", "/")
    response = connection.getresponse()
    html = response.read().decode("utf-8")
    connection.close()
    assert response.status == 200
    assert 'window.__LOCAL_TOKEN__ = "test-token"' in html
    assert "__LOCAL_TOKEN_VALUE__" not in html
    assert '<link rel="icon" href="data:," />' in html


def test_post_rejects_invalid_token(running_server: RunningServer) -> None:
    status, payload = running_server.request("POST", "/api/action", body={"action": "demo"}, token="wrong")
    assert status == 403
    assert "令牌" in payload["error"]


def test_post_rejects_unknown_action(running_server: RunningServer) -> None:
    status, payload = running_server.request("POST", "/api/action", body={"action": "run-anything"})
    assert status == 400
    assert "不支持的操作" in payload["error"]


def test_codex_requires_confirmation_before_report_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = web_app.TaskManager(REPO_ROOT, popen_factory=lambda *args, **kwargs: pytest.fail("must not run"))
    with pytest.raises(web_app.WebAppError, match="消耗账户用量"):
        manager.start("codex_review", confirm_usage=False)


def test_codex_requires_a_real_report(tmp_path: Path) -> None:
    manager = web_app.TaskManager(tmp_path, popen_factory=lambda *args, **kwargs: pytest.fail("must not run"))
    with pytest.raises(web_app.WebAppError, match="真实研究报告"):
        manager.start("codex_review", confirm_usage=True)


def test_fixed_command_mapping_does_not_accept_arbitrary_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for filename in ("run-demo.ps1", "run-real.ps1", "run-codex-review.ps1"):
        (tmp_path / filename).write_text("", encoding="utf-8")
    monkeypatch.setattr(web_app, "_find_powershell", lambda: "powershell.exe")

    assert web_app.command_for_action("demo", tmp_path)[-1] == str(tmp_path / "run-demo.ps1")
    assert web_app.command_for_action("capture_close", tmp_path)[-3:] == [str(tmp_path / "run-real.ps1"), "-Mode", "CaptureClose"]
    with pytest.raises(web_app.WebAppError, match="不支持的操作"):
        web_app.command_for_action("run-anything", tmp_path)


def test_codex_command_is_bound_to_latest_real_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True)
    report = reports / "real-report-20260901-150000.md"
    report.write_text("真实公开行情", encoding="utf-8")
    (tmp_path / "run-codex-review.ps1").write_text("", encoding="utf-8")
    monkeypatch.setattr(web_app, "_find_powershell", lambda: "powershell.exe")
    command = web_app._codex_command(tmp_path, report)
    assert command[-3:] == ["-ReportPath", str(report.resolve()), "-Yes"]
    with pytest.raises(web_app.WebAppError, match="固定报告目录"):
        web_app._codex_command(tmp_path, tmp_path / "outside.md")


def test_task_manager_enforces_one_running_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "run-demo.ps1").write_text("", encoding="utf-8")
    monkeypatch.setattr(web_app, "_find_powershell", lambda: "powershell.exe")
    release = threading.Event()

    class FakeProcess:
        returncode = 0

        def communicate(self):
            release.wait(timeout=3)
            return b"done", None

    manager = web_app.TaskManager(tmp_path, popen_factory=lambda *args, **kwargs: FakeProcess())
    first = manager.start("demo")
    assert first.status == "running"
    with pytest.raises(web_app.TaskBusyError, match="已有任务"):
        manager.start("demo")
    release.set()
    for _ in range(30):
        if manager.current() is not None and manager.current().status != "running":
            break
        time.sleep(0.01)
    assert manager.current() is not None
    assert manager.current().status == "succeeded"


def test_static_path_traversal_is_not_served(running_server: RunningServer) -> None:
    for path in ("/../run-real.ps1", "/%2e%2e/run-real.ps1", "/data/reports/real-report.md"):
        connection = http.client.HTTPConnection("127.0.0.1", running_server.port, timeout=3)
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 404


def test_launcher_files_contain_hidden_server_and_explicit_chrome_behavior() -> None:
    launcher = (REPO_ROOT / "run-web.ps1").read_text(encoding="utf-8")
    cmd = (REPO_ROOT / "run-web.cmd").read_text(encoding="utf-8")
    assert "pythonw.exe" in launcher
    assert "Start-Process" in launcher
    assert "-WindowStyle Hidden" in launcher
    assert "chrome.exe" in launcher
    assert "127.0.0.1:8765" in launcher
    assert "run-web.ps1" in cmd
    assert "powershell.exe" in cmd


def test_cmd_launcher_is_ascii_crlf_and_real_cmd_parses_exit_code(tmp_path: Path) -> None:
    cmd_source = REPO_ROOT / "run-web.cmd"
    raw = cmd_source.read_bytes()
    assert raw
    assert all(byte < 128 for byte in raw)
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert b"if errorlevel 1" in raw
    assert b"-ExecutionPolicy" in raw
    assert b"exit /b %EXIT_CODE%" in raw

    powershell = shutil.which("powershell.exe")
    command_shell = os.environ.get("ComSpec") or shutil.which("cmd.exe")
    if powershell is None or command_shell is None:
        pytest.skip("需要 Windows cmd.exe 和 PowerShell 才能执行批处理解析回归测试")

    (tmp_path / "run-web.cmd").write_bytes(raw)
    # The real launcher is called by cmd.exe, but this stand-in exits before
    # any server or Chrome work, making the parser/exit-code test deterministic.
    (tmp_path / "run-web.ps1").write_text("exit 17\n", encoding="utf-8")
    result = subprocess.run(
        [command_shell, "/d", "/c", str(tmp_path / "run-web.cmd")],
        cwd=tmp_path,
        input="\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 17
    assert "Web launcher failed. Exit code: 17." in result.stdout + result.stderr


def test_results_folder_is_the_data_root_and_codex_folder_stays_separate() -> None:
    assert web_app.FOLDER_PATHS["data"] == REPO_ROOT / "data"
    assert web_app.FOLDER_PATHS["codex_reviews"] == REPO_ROOT / "data" / "codex_reviews"
    html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-folder="data"' in html
    assert "打开全部结果文件夹" in html
    assert "演示、快照与研究报告" in html


def test_open_data_folder_uses_fixed_data_root(monkeypatch: pytest.MonkeyPatch, running_server: RunningServer) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web_app.os, "startfile", lambda path: opened.append(path), raising=False)
    status, payload = running_server.request("POST", "/api/open-folder", body={"folder": "data"})
    assert status == 200
    assert payload == {"ok": True, "folder": "data"}
    assert opened == [str((REPO_ROOT / "data").resolve())]


def test_mobile_layout_constrains_long_result_names() -> None:
    styles = (REPO_ROOT / "web" / "styles.css").read_text(encoding="utf-8")
    status_card_rule = styles.split(".status-card {", 1)[1].split("}", 1)[0]
    status_value_rule = styles.split(".status-value {", 1)[1].split("}", 1)[0]
    result_rule = styles.split(".result-line code {", 1)[1].split("}", 1)[0]
    assert "min-width: 0" in status_card_rule
    assert "min-width: 0" in status_value_rule
    assert "min-width: 0" in result_rule
    assert "flex: 1" in result_rule
