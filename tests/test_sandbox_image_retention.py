"""验证部署重建镜像前保留旧沙箱镜像，以及首次部署和构建跳过边界。"""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def deploy_all(monkeypatch: pytest.MonkeyPatch):
    """加载仅依赖标准库的真实部署入口，隔离 Docker 命令执行。"""
    spec = importlib.util.spec_from_file_location("yp_test_deploy_all_images", ROOT / "scripts/deploy_all.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/docker")
    return module


@pytest.mark.parametrize(("existing", "skip_build"), [(True, False), (False, False), (True, True)])
def test_deployment_retains_image_before_replacing_tag(deploy_all, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, skip_build: bool) -> None:
    """正式部署流程必须在移动可变标签前保留旧镜像，并保留新构建镜像。"""
    image = "example/sandbox:1"
    previous, current = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    references = {}
    if existing:
        references[image] = previous
    config = {
        "services": {
            "api": {
                "environment": {
                    "SCRIPT_SANDBOX_IMAGE": image,
                },
            },
            "web": {
                "environment": {
                    "YPOINT_HTTPS_ENABLED": "false",
                },
                "ports": [
                    {
                        "target": 8080,
                        "published": "18080",
                        "host_ip": "127.0.0.1",
                    },
                ],
            },
        },
    }

    def docker_command(command, *, capture=False):
        """模拟 Docker 标签移动，检查删除旧标签前已有独立引用。"""
        if command[:3] == ["docker", "image", "inspect"]:
            if command[3] not in references:
                raise subprocess.CalledProcessError(1, command, stderr="Error response from daemon: No such image")
            return references[command[3]] + "\n"
        if command[:3] == ["docker", "image", "tag"]:
            references[command[4]] = command[3]
        elif command[:2] == ["docker", "build"]:
            assert not skip_build
            if existing:
                assert references.get("y-point-script-sandbox-pinned:" + "a" * 64) == previous
            references[image] = current
        elif "config" in command:
            return json.dumps(config)
        return ""

    monkeypatch.setattr(deploy_all, "run", docker_command)
    deploy_all.main(["--sandbox", "--directory", str(tmp_path), *(["--skip-build"] if skip_build else [])])
    expected = previous if skip_build else current
    assert references["y-point-script-sandbox-pinned:" + expected.removeprefix("sha256:")] == expected
    if existing:
        assert references["y-point-script-sandbox-pinned:" + "a" * 64] == previous


def test_retention_does_not_ignore_docker_access_failure(deploy_all, monkeypatch: pytest.MonkeyPatch) -> None:
    """允许首次缺少镜像时，也不能把 Docker 权限错误当成镜像不存在。"""

    def denied_command(command, *, capture=False):
        """模拟 Docker daemon 权限错误。"""
        raise subprocess.CalledProcessError(1, command, stderr="permission denied")

    monkeypatch.setattr(deploy_all, "run", denied_command)
    with pytest.raises(subprocess.CalledProcessError):
        deploy_all.retain_sandbox_image("example/sandbox:1", allow_missing=True)


@pytest.mark.skipif(shutil.which("docker") is None, reason="需要本机 Docker")
def test_retained_image_survives_real_docker_tag_replacement(deploy_all) -> None:
    """用两个独立空镜像验证更新可变标签后旧镜像仍有可解析引用，不执行全局清理。"""
    available = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    if available.returncode:
        pytest.skip("Docker daemon is unavailable")
    source = f"y-point-retention-test:{uuid4().hex}"
    retained = None
    try:
        for index in range(2):
            subprocess.run(
                ["docker", "build", "--quiet", "--tag", source, "-"],
                input=f"FROM scratch\nLABEL y-point.retention-test={uuid4().hex}\n",
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )
            identifier = subprocess.check_output(["docker", "image", "inspect", source, "--format", "{{.Id}}"], text=True).strip()
            if index == 0:
                previous = identifier
                deploy_all.retain_sandbox_image(source)
                retained = "y-point-script-sandbox-pinned:" + previous.removeprefix("sha256:")
            else:
                assert identifier != previous
        original = subprocess.check_output(["docker", "image", "inspect", retained, "--format", "{{.Id}} {{json .RepoTags}}"], text=True)
        assert original.startswith(previous + " ")
        assert retained in original and source not in original
    finally:
        subprocess.run(["docker", "image", "rm", source, *([retained] if retained else [])], capture_output=True, timeout=15)
