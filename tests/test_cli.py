"""验证统一 CLI 的安装入口、标准库隔离、仓库定位和 Compose 参数边界。"""

from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from ypoint_cli.main import main

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("command", [[], ["dev"], ["deploy"], ["init"], ["compose"], ["verify-deployment"]])
def test_help_requires_only_standard_library(command: list[str], tmp_path: Path) -> None:
    """关闭 site-packages 后所有帮助仍可用，且不查找仓库或生成配置。"""
    script = "import sys; sys.path.insert(0, sys.argv.pop(1)); from ypoint_cli.main import main; raise SystemExit(main(sys.argv[1:]))"
    result = subprocess.run([sys.executable, "-S", "-c", script, str(ROOT), *command, "--help"], cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "usage: yp" in result.stdout
    assert not list(tmp_path.iterdir())


def test_installed_entry_and_external_working_directory(tmp_path: Path) -> None:
    """真实 console script 可从其他目录定位源码并初始化自定义私有目录。"""
    executable = Path(sys.executable).parent / "yp"
    directory = tmp_path / "配置 directory"
    result = subprocess.run(
        [str(executable), "--project-dir", str(ROOT), "init", "--directory", str(directory)], cwd=tmp_path, text=True, capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    original = (directory / "application.env").read_bytes()
    assert (directory / "application.env").stat().st_mode & 0o077 == 0
    again = subprocess.run(
        [str(executable), "--project-dir", str(ROOT), "init", "--directory", str(directory)], cwd=tmp_path, text=True, capture_output=True, timeout=15
    )
    assert again.returncode == 0, again.stderr
    assert (directory / "application.env").read_bytes() == original


def test_compose_forwards_arguments_and_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """分隔符后与 yp 同名的参数仍原样交给 Docker，返回真实退出码。"""
    from ypoint_cli import deployment

    directory = tmp_path / "private"
    directory.mkdir()
    (directory / "compose.env").write_text("YPOINT_HTTPS_ENABLED=false\n")
    seen = []

    def invoke(command, *, cwd):
        seen.append((command, cwd))
        return 17

    monkeypatch.setattr(deployment.subprocess, "call", invoke)
    with pytest.raises(SystemExit) as failure:
        main(["--project-dir", str(ROOT), "compose", "--directory", str(directory), "--sandbox", "--", "exec", "api", "echo", "--environment", "production"])
    assert failure.value.code == 17
    command, cwd = seen[0]
    assert command[:2] == ["docker", "compose"] and cwd == ROOT
    assert command[-5:] == ["exec", "api", "echo", "--environment", "production"]
    assert str(ROOT / "docker-compose.sandbox.yml") in command
    assert str(ROOT / "docker-compose.production.yml") not in command


def test_invalid_project_and_parameters_do_not_initialize(tmp_path: Path, capsys) -> None:
    """错误源码目录和参数在执行前失败，不在任意工作目录生成部署文件。"""
    assert main(["--project-dir", str(tmp_path), "deploy"]) == 1
    assert "项目目录不完整" in capsys.readouterr().err
    with pytest.raises(SystemExit) as failure:
        main(["--project-dir", str(ROOT), "init", "--directory", str(tmp_path / "private"), "--unknown"])
    assert failure.value.code == 2
    assert not (tmp_path / "private").exists()


def test_cli_package_configuration_is_explicit() -> None:
    """安装项目才能生成命令，并明确排除私有数据和运维资源目录。"""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["project"]["scripts"] == {"yp": "ypoint_cli.main:main"}
    assert config["tool"]["uv"]["package"] is True
    assert config["tool"]["setuptools"]["packages"]["find"]["include"] == ["app", "app.*", "ypoint_cli", "ypoint_cli.*"]
