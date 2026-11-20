"""Y-Point 开发与 Docker 部署的统一命令入口。"""

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import subprocess
import sys
import tomllib


def project_root(explicit: Path | None = None) -> Path:
    """从显式目录、工作目录或安装位置查找完整源码仓库。"""

    def matches(path: Path) -> bool:
        manifest = path / "pyproject.toml"
        if not manifest.is_file() or not (path / "docker-compose.yml").is_file() or not (path / "deploy/config/application.env.example").is_file():
            return False
        try:
            return tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {}).get("name") == "y-point"
        except (OSError, ValueError):
            return False

    if explicit is not None:
        root = explicit.expanduser().resolve()
        if matches(root):
            return root
        raise ValueError(f"项目目录不完整：{root}；需要 Y-Point 源码、Compose 与 deploy 配置模板")
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for candidate in (base, *base.parents):
            if matches(candidate):
                return candidate
    raise ValueError("未找到 Y-Point 源码仓库；请进入项目目录，或使用 yp --project-dir /path/to/y-point <子命令>")


def main(argv: list[str] | None = None) -> int:
    """解析公共选项，再交由对应模块处理参数；保留子命令的退出状态。"""
    try:
        release = version("y-point")
    except PackageNotFoundError:
        release = "0.1.0"
    parser = argparse.ArgumentParser(
        prog="yp",
        description=__doc__,
        epilog=(
            "子命令：dev（本地开发）、deploy（一键部署）、init（初始化配置）、"
            "compose（Compose 操作）、verify-deployment（部署检查）。使用 yp <子命令> --help 查看详细参数。"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {release}")
    parser.add_argument("--project-dir", type=Path, help="Y-Point 源码仓库根目录；默认从当前目录或安装位置向上查找，放在子命令之前")
    parser.add_argument("command", nargs="?", choices=("dev", "deploy", "init", "compose", "verify-deployment"), help="要执行的操作")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    # 帮助不需要仓库、Docker、配置或业务依赖。
    help_only = args.arguments == ["--help"] or args.arguments == ["-h"]
    try:
        root = Path.cwd() if help_only else project_root(args.project_dir)
        if args.command == "dev":
            dev_parser = argparse.ArgumentParser(
                prog="yp dev", description="启动并管理本项目的 API、Worker 和 Web；Ctrl-C 停止三项服务，重复启动会接管旧开发服务。"
            )
            dev_parser.parse_args(args.arguments)
            from ypoint_cli.dev import main as run_dev

            return run_dev(root=root)
        if args.command == "deploy":
            from ypoint_cli.deploy import main as deploy

            deploy(args.arguments, root=root)
        elif args.command in {"init", "compose"}:
            from ypoint_cli.deployment import main as deployment

            deployment([args.command, *args.arguments], root=root)
        else:
            from ypoint_cli.verify import main as verify

            verify(args.arguments, root=root)
        return 0
    except subprocess.CalledProcessError as error:
        print(f"yp: 命令退出码 {error.returncode}", file=sys.stderr)
        return error.returncode if error.returncode > 0 else 128 - error.returncode
    except (OSError, ValueError, RuntimeError) as error:
        print(f"yp: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
