# -*- coding: utf-8 -*-
# @Time    : 2026-11-14 17:48:57
# @Author  : yangyuexiong
# @File    : test_deploy_all.py

"""验证一键部署的命令顺序、选项透传、私密输出和失败即停行为。"""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import deploy_all


@pytest.fixture
def docker_probe(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[list[str], dict]], dict]:
    """替代 Docker 命令边界，记录真实编排参数并提供包含秘密的已解析配置。"""
    calls = []
    config = {
        "services": {
            "api": {
                "environment": {
                    "SCRIPT_SANDBOX_IMAGE": "custom-script-sandbox:9",
                    "SECRET_KEY": "must-never-print-this-secret",
                },
            },
            "web": {
                "environment": {
                    "YPOINT_HTTPS_ENABLED": "false",
                },
                "ports": [
                    {
                        "host_ip": "0.0.0.0",
                        "published": "19080",
                        "target": 8080,
                    },
                ],
            },
        },
    }

    def invoke(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        """保留实际 run 包装器，模拟其子进程返回以检查输出捕获参数。"""
        calls.append((list(command), kwargs))
        output = json.dumps(config) if command[-3:] == ["config", "--format", "json"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(deploy_all.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(deploy_all.subprocess, "run", invoke)
    return calls, config


def test_one_click_orders_steps_and_hides_private_configuration(docker_probe, tmp_path: Path, capsys) -> None:
    """所有步骤使用同一环境和目录，配置不泄露，访问地址来自解析后的 Compose。"""
    calls, _ = docker_probe
    directory = tmp_path / "private directory"
    deploy_all.main(["--environment", "production", "--directory", str(directory), "--http", "--bind-ip", "0.0.0.0", "--http-port", "19080"])
    commands = [command for command, _ in calls]
    assert commands[0] == ["docker", "compose", "version", "--short"]
    assert commands[1][:2] == ["docker", "info"]
    assert commands[2][2:5] == ["init", "--environment", "production"]
    assert commands[2][-5:] == ["--http", "--bind-ip", "0.0.0.0", "--http-port", "19080"]
    assert commands[3][-3:] == ["config", "--format", "json"]
    assert calls[3][1]["capture_output"] is True
    assert commands[4][-1] == "build"
    assert commands[5][-6:] == ["up", "-d", "--no-build", "--wait", "--wait-timeout", "180"]
    assert commands[6][-1] == "ps"
    assert all(str(directory) in command for command in commands[2:])
    assert all(options["cwd"] == deploy_all.ROOT for _, options in calls)
    output = capsys.readouterr().out
    assert "http://<服务器IP>:19080" in output
    assert "must-never-print-this-secret" not in output
    assert "app.cli.create_admin" in output
    assert not any("app.cli.create_admin" in command for command in commands)


@pytest.mark.parametrize("skip_build", [False, True])
def test_sandbox_uses_configured_image_and_https_arguments(docker_probe, tmp_path: Path, skip_build: bool, capsys) -> None:
    """按实际应用配置准备沙箱镜像，并将 HTTPS 与沙箱参数透传给同一部署。"""
    calls, config = docker_probe
    web = config["services"]["web"]
    web["environment"]["YPOINT_HTTPS_ENABLED"] = "true"
    web["ports"].append(
        {
            "host_ip": "192.168.1.10",
            "published": "19443",
            "target": 8443,
        },
    )
    certificate = tmp_path / "certificate directory" / "tls.crt"
    key = tmp_path / "certificate directory" / "tls.key"
    arguments = ["--https", "--tls-cert", str(certificate), "--tls-key", str(key), "--sandbox", "--wait-timeout", "300"]
    deploy_all.main([*arguments, *(["--skip-build"] if skip_build else [])])
    commands = [command for command, _ in calls]
    initialize = commands[2]
    assert "--https" in initialize and str(certificate) in initialize and str(key) in initialize
    compose = [command for command in commands if "compose" in command and "--environment" in command]
    assert all("--sandbox" in command for command in compose)
    if skip_build:
        assert ["docker", "image", "inspect", "custom-script-sandbox:9"] in commands
        assert not any(command[-1] == "build" or command[:2] == ["docker", "build"] for command in commands)
    else:
        sandbox = next(command for command in commands if command[:2] == ["docker", "build"])
        assert sandbox[sandbox.index("-t") + 1] == "custom-script-sandbox:9"
        assert sandbox[sandbox.index("-f") + 1] == str(deploy_all.ROOT / "Dockerfile.sandbox")
    assert any(command[-2:] == ["--wait-timeout", "300"] for command in commands)
    assert "https://192.168.1.10:19443" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["info", "build", "up"])
def test_failed_step_stops_without_cleanup(docker_probe, monkeypatch: pytest.MonkeyPatch, failure: str, capsys) -> None:
    """检查、构建或启动失败均返回原失败码，不继续后续步骤或自动删除数据。"""
    calls, _ = docker_probe
    invoke = deploy_all.subprocess.run

    def fail(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        """在指定命令边界模拟失败，保留前面成功命令的记录。"""
        result = invoke(command, **kwargs)
        if (failure == "info" and command[:2] == ["docker", "info"]) or command[-1] == failure or (failure == "up" and "up" in command):
            raise subprocess.CalledProcessError(7, command)
        return result

    monkeypatch.setattr(deploy_all.subprocess, "run", fail)
    with pytest.raises(SystemExit) as error:
        deploy_all.main([])
    assert error.value.code == 7
    commands = [command for command, _ in calls]
    assert commands[-1][-1] != "ps"
    assert not any("down" in command or "rm" in command or "app.cli.create_admin" in command for command in commands)
    output = capsys.readouterr()
    assert "已保留配置、数据和容器" in output.err
    assert "部署完成" not in output.out


def test_admin_creation_runs_only_after_health_wait(docker_probe, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式请求时才在服务就绪后进入密码交互，不接受命令行密码。"""
    calls, _ = docker_probe
    monkeypatch.setattr(deploy_all.sys.stdin, "isatty", lambda: True)
    deploy_all.main(["--create-admin", "admin"])
    commands = [command for command, _ in calls]
    assert commands[-2][-1] == "ps"
    assert commands[-1][-6:] == ["exec", "api", "python", "-m", "app.cli.create_admin", "admin"]
    assert calls[-1][1]["capture_output"] is False


def test_noninteractive_admin_request_fails_before_mutation(docker_probe, monkeypatch: pytest.MonkeyPatch) -> None:
    """非交互终端应在部署前拒绝管理员密码交互，不留下半完成的初始化。"""
    calls, _ = docker_probe
    monkeypatch.setattr(deploy_all.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as error:
        deploy_all.main(["--create-admin", "admin"])
    assert error.value.code == 2
    assert not calls
