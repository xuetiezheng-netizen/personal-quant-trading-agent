"""Windows/命令行入口：运行本机持仓的中低频波段观察或历史模拟。

默认只读取仓库 ``data/private/holdings.json``，只读公开日线，并将报告和
运行摘要写回 ``data/private``。它没有券商连接、下单接口，也不会读取模型
密钥。错误输出保持简短，不回显绝对路径或持仓原始内容。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trading_agent.swing.service import (
    SwingRun,
    SwingService,
)


def _parse_as_of(value: str) -> date | datetime:
    text = value.strip()
    try:
        if "T" in text or " " in text:
            return datetime.fromisoformat(text)
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("as-of 必须是 YYYY-MM-DD 或 ISO 日期时间") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本机持仓中低频日线观察/历史模拟（只读公开行情，不连接券商）"
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("analyze", "backtest", "all"),
        help="analyze=观察状态，backtest=历史模拟，all=两者都运行",
    )
    parser.add_argument(
        "--action",
        choices=("analyze", "backtest", "all"),
        help="网页入口使用的等价模式参数",
    )
    parser.add_argument(
        "--code",
        help="仅处理一个已存在于本机持仓文件的6位代码；不提供则处理全部",
    )
    parser.add_argument(
        "--asset-type",
        choices=("stock", "etf"),
        help="与 --code 一起用于校验资产类别",
    )
    parser.add_argument(
        "--as-of",
        type=_parse_as_of,
        help="测试或复盘截止时点；省略则使用当前北京时间",
    )
    # 仅供测试在临时仓库运行；生产入口仍默认为脚本所在仓库，且服务层
    # 会把所有写入固定到该仓库 data/private 下。
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def _read_repo_tushare_token(repo_root: Path) -> str:
    """Read only ``TUSHARE_TOKEN`` from the explicitly selected repo ``.env``.

    The command-line entry point intentionally does not call ``load_dotenv``:
    that would inject unrelated values (for example model credentials) into
    the process.  ``dotenv_values`` parses the file without mutating the
    environment, and the whitelist below keeps the optional data credential
    isolated from every other setting.
    """

    try:
        values = dotenv_values(repo_root / ".env")
    except (OSError, UnicodeError, ValueError):
        return ""
    token = values.get("TUSHARE_TOKEN")
    return token.strip() if isinstance(token, str) else ""


def _build_service(repo_root: Path) -> SwingService:
    """Build the default service with a narrowly scoped optional token.

    A non-empty process environment value wins.  When it is absent/blank, a
    token from this exact repository's ``.env`` is temporarily exposed only
    while ``SwingService`` constructs its default provider.  It is restored
    immediately afterwards so the entry point does not leave secrets in the
    caller's environment.
    """

    process_token = os.environ.get("TUSHARE_TOKEN", "")
    if isinstance(process_token, str) and process_token.strip():
        return SwingService(repo_root)

    repo_token = _read_repo_tushare_token(repo_root)
    if not repo_token:
        return SwingService(repo_root)

    previous = os.environ.get("TUSHARE_TOKEN")
    os.environ["TUSHARE_TOKEN"] = repo_token
    try:
        return SwingService(repo_root)
    finally:
        if previous is None:
            os.environ.pop("TUSHARE_TOKEN", None)
        else:
            os.environ["TUSHARE_TOKEN"] = previous


def _display_mode(mode: str) -> str:
    return {"analyze": "观察", "backtest": "历史模拟", "all": "观察与历史模拟"}[mode]


def _print_run(run: SwingRun) -> None:
    print(f"已完成：{_display_mode(run.mode)}，共处理 {len(run.results)} 个结果。")
    if not run.results:
        print("本机持仓为空，未生成报告。")
        return
    counts: dict[str, int] = {}
    for item in run.results:
        counts[item.status] = counts.get(item.status, 0) + 1
    print("结果状态：" + "、".join(f"{key} {value} 项" for key, value in sorted(counts.items())))
    for item in run.results:
        if item.status == "ok":
            print(f"- {item.code}：本次结果成功。")
        else:
            print(f"- {item.code}：本次结果失败；若有上次成功结果，最新摘要会保留旧结果并标注本次失败。")
    print("报告已保存到本机私有目录 data/private/reports。")


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[Path], SwingService] | None = None,
) -> int:
    """命令行主函数；``service_factory`` 只用于测试注入模拟 provider。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.mode and args.action and args.mode != args.action:
            parser.error("mode 与 --action 不得冲突")
        mode = args.action or args.mode
        if mode is None:
            parser.error("必须提供 mode 或 --action")
        if args.asset_type is not None and args.code is None:
            parser.error("--asset-type 必须与 --code 一起使用")
        service = (service_factory or _build_service)(args.repo_root)
        run = service.run(
            mode,
            code=args.code,
            asset_type=args.asset_type,
            as_of=args.as_of,
        )
        _print_run(run)
        if run.results and not any(item.status == "ok" for item in run.results):
            return 2
        return 0
    except (RuntimeError, OSError, ValueError):
        # 不打印异常对象：provider/路径错误可能包含请求、绝对路径或个人字段。
        print("持仓波段运行失败：请检查本机私有持仓文件和公开日线是否可用。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Web TaskManager 按 UTF-8 读取子进程日志；显式统一编码，避免 Windows
    # 本地代码页把中文完成信息变成乱码。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
