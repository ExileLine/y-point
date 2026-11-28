"""验证沙箱镜像保留、部署成功后显式清理，以及失败时不提前删除的边界。"""

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
    spec = importlib.util.spec_from_file_location("yp_test_deploy_all_images", ROOT / "ypoint_cli/deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(module.deployment, "main", lambda *args, **kwargs: None)
    monkeypatch.setattr(module.deployment, "compose_command", lambda *args, **kwargs: ["docker", "compose"])
    return module


@pytest.mark.parametrize(("existing", "skip_build"), [(True, False), (False, False), (True, True)])
@pytest.mark.parametrize("prune", [False, True])
def test_deployment_retains_image_before_replacing_tag(
    deploy_all,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    skip_build: bool,
    prune: bool,
) -> None:
    """构建前保留旧镜像，仅在显式请求且服务健康后清理旧标签，兼容首次部署和跳过构建。"""
    image = "example/sandbox:1"
    previous, current = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    references = {}
    commands = []
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

    def docker_command(command, *, capture=False, root=None):
        """模拟 Docker 标签移动，检查删除旧标签前已有独立引用。"""
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            if command[3] not in references:
                raise subprocess.CalledProcessError(1, command, stderr="Error response from daemon: No such image")
            return references[command[3]] + "\n"
        if command[:3] == ["docker", "image", "tag"]:
            references[command[4]] = command[3]
        elif command[:3] == ["docker", "image", "ls"]:
            return "\n".join(reference for reference in references if reference.startswith("y-point-script-sandbox-pinned:"))
        elif command[:3] == ["docker", "image", "rm"]:
            assert any("up" in previous_command and "--wait" in previous_command for previous_command in commands)
            assert any(previous_command[-1] == "ps" for previous_command in commands)
            del references[command[3]]
        elif command[:2] == ["docker", "build"]:
            assert not skip_build
            if existing:
                assert references.get("y-point-script-sandbox-pinned:" + "a" * 64) == previous
            references[image] = current
        elif "config" in command:
            return json.dumps(config)
        return ""

    monkeypatch.setattr(deploy_all, "run", docker_command)
    deploy_all.main(["--sandbox", "--directory", str(tmp_path), *(["--skip-build"] if skip_build else []), *(["--prune-sandbox-images"] if prune else [])])
    expected = previous if skip_build else current
    assert references["y-point-script-sandbox-pinned:" + expected.removeprefix("sha256:")] == expected
    if existing:
        old_tag = "y-point-script-sandbox-pinned:" + "a" * 64
        if prune and not skip_build:
            assert old_tag not in references
        else:
            assert references[old_tag] == previous
    if not prune:
        assert not any(command[:3] == ["docker", "image", "ls"] or command[:3] == ["docker", "image", "rm"] for command in commands)


def test_prune_preserves_actual_current_image_and_unmanaged_tags(deploy_all, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """按真实 ID 保留当前镜像，重复标签去重，忽略非受管标签且只按标签删除。"""
    current, previous = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    current_tag = "y-point-script-sandbox-pinned:" + "a" * 64
    old_tag = "y-point-script-sandbox-pinned:" + "b" * 64
    alias = "y-point-script-sandbox-pinned:" + "c" * 64
    calls = []
    identities = {
        "custom/sandbox:9": current,
        current_tag: current,
        old_tag: previous,
        alias: current,
    }

    def docker_command(command, *, capture=False, root=None):
        """模拟含当前镜像别名及非受管标签的列表，并记录删除边界。"""
        calls.append(command)
        assert root == tmp_path
        if command[:3] == ["docker", "image", "inspect"]:
            return identities[command[3]]
        if command[:3] == ["docker", "image", "ls"]:
            return "\n".join([old_tag, current_tag, old_tag, alias, "y-point-script-sandbox-pinned:manual", "unrelated:1"])
        return ""

    monkeypatch.setattr(deploy_all, "run", docker_command)
    deploy_all.prune_sandbox_images("custom/sandbox:9", root=tmp_path)
    assert [command for command in calls if command[:3] == ["docker", "image", "rm"]] == [["docker", "image", "rm", old_tag]]


@pytest.mark.parametrize("failure", ["current_id", "current_inspect", "listing", "old_id", "old_inspect"])
def test_prune_checks_all_candidates_before_removal(deploy_all, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """镜像 ID 无效或任何预检失败时，不删除已发现的历史标签。"""
    first = "y-point-script-sandbox-pinned:" + "b" * 64
    second = "y-point-script-sandbox-pinned:" + "c" * 64
    calls = []

    def docker_command(command, *, capture=False, root=None):
        """在当前镜像、枚举或第二个历史镜像的检查阶段注入故障。"""
        calls.append(command)
        if command[:3] == ["docker", "image", "ls"]:
            if failure == "listing":
                raise subprocess.CalledProcessError(7, command)
            return first + "\n" + second
        if command[:3] == ["docker", "image", "inspect"]:
            if command[3] == "custom/sandbox:9":
                if failure == "current_inspect":
                    raise subprocess.CalledProcessError(7, command)
                return "invalid" if failure == "current_id" else "sha256:" + "a" * 64
            if command[3] == second:
                if failure == "old_inspect":
                    raise subprocess.CalledProcessError(7, command)
                if failure == "old_id":
                    return "invalid"
            return "sha256:" + command[3].split(":")[1]
        return ""

    monkeypatch.setattr(deploy_all, "run", docker_command)
    with pytest.raises((SystemExit, subprocess.CalledProcessError)):
        deploy_all.prune_sandbox_images("custom/sandbox:9")
    assert not any(command[:3] == ["docker", "image", "rm"] for command in calls)


def test_prune_stops_on_docker_removal_failure(deploy_all, monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker 拒绝删除时原样抛出错误，不强删或继续删除后续镜像。"""
    tags = ["y-point-script-sandbox-pinned:" + character * 64 for character in "bc"]
    calls = []

    def docker_command(command, *, capture=False, root=None):
        """模拟无当前保留标签的历史列表与首个删除冲突。"""
        calls.append(command)
        if command[:3] == ["docker", "image", "ls"]:
            return "\n".join(tags)
        if command[:3] == ["docker", "image", "rm"]:
            raise subprocess.CalledProcessError(9, command)
        return "sha256:" + ("a" * 64 if command[3] == "custom/sandbox:9" else command[3].split(":")[1])

    monkeypatch.setattr(deploy_all, "run", docker_command)
    with pytest.raises(subprocess.CalledProcessError) as error:
        deploy_all.prune_sandbox_images("custom/sandbox:9")
    assert error.value.returncode == 9
    assert [command for command in calls if command[:3] == ["docker", "image", "rm"]] == [["docker", "image", "rm", tags[0]]]


def test_retention_does_not_ignore_docker_access_failure(deploy_all, monkeypatch: pytest.MonkeyPatch) -> None:
    """允许首次缺少镜像时，也不能把 Docker 权限错误当成镜像不存在。"""

    def denied_command(command, *, capture=False, root=None):
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
