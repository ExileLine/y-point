# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : script_sandbox.py

"""通过 Docker 运行仅接收 JSON 输入的用户脚本。"""

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.core.enums import ScriptLanguage
from app.services.tool_resources import managed_workspace, resource_labels
from app.core.config import get_config

MAX_OUTPUT_BYTES = 64 * 1024
SANDBOX_ROOT = Path(__file__).resolve().parents[2] / ".run-private" / "tool-sandbox"


@dataclass(frozen=True)
class ScriptEnvironment:
    """指定已校验的固定镜像和只读依赖目录。"""

    image: str
    site: Path


class SandboxConfigurationError(ValueError):
    """表示可向 Tool 所有者公开的依赖或镜像配置问题。"""


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    """读取受限输出，超过上限立即中止调用。"""
    result = bytearray()
    while chunk := await stream.read(4096):
        result.extend(chunk)
        if len(result) > MAX_OUTPUT_BYTES:
            raise ValueError("脚本输出超过 64 KiB")
    return bytes(result)


async def _remove_container(name: str) -> None:
    """在超时或取消后强制移除本次容器。"""
    try:
        cleanup = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(cleanup.wait(), timeout=3)
    except (OSError, TimeoutError):
        pass


async def run_script(
    language: str, source: str, arguments: dict, timeout_seconds: int, *, environment: ScriptEnvironment | None = None
) -> str:
    """在无网络、只读、非特权容器中执行脚本并返回有界 JSON 结果。"""
    if language not in ScriptLanguage or timeout_seconds <= 0:
        raise ValueError("脚本运行配置无效")
    input_bytes = json.dumps(arguments, ensure_ascii=False).encode()
    if len(input_bytes) > 64 * 1024:
        raise ValueError("脚本输入超过 64 KiB")
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = "main.py" if language == ScriptLanguage.PYTHON else "main.sh"
    container_name = f"yp-tool-{uuid4().hex}"
    with managed_workspace(SANDBOX_ROOT, "run-", timeout_seconds) as directory:
        script = Path(directory) / filename
        script.write_text(source, encoding="utf-8")
        os.chmod(script, 0o644)
        interpreter = (
            [ScriptLanguage.PYTHON, "-B", f"/app/{filename}"]
            if language == ScriptLanguage.PYTHON
            else [ScriptLanguage.BASH, f"/app/{filename}"]
        )
        dependency_mounts = []
        if environment is not None:
            if language != ScriptLanguage.PYTHON:
                raise ValueError("依赖环境仅支持 Python 脚本")
            dependency_mounts = [
                "--mount",
                f"type=bind,src={environment.site},dst=/opt/tool-deps,readonly",
                "--env",
                "PYTHONPATH=/opt/tool-deps",
            ]
            interpreter = [
                "python",
                "-B",
                "-c",
                "import site, runpy; site.addsitedir('/opt/tool-deps'); runpy.run_path('/app/main.py', run_name='__main__')",
            ]
        command = ["timeout", "-s", "KILL", str(timeout_seconds + 2), *interpreter]
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--name",
            container_name,
            *resource_labels(timeout_seconds),
            "--interactive",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=128m",
            "--cpus=1",
            "--user=65534:65534",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=8m",
            "--mount",
            f"type=bind,src={script},dst=/app/{filename},readonly",
            "--workdir=/app",
            *dependency_mounts,
            environment.image if environment is not None else get_config().SCRIPT_SANDBOX_IMAGE,
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            process.stdin.write(input_bytes)
            await asyncio.wait_for(process.stdin.drain(), timeout=timeout_seconds)
            process.stdin.close()
            output, errors, _ = await asyncio.wait_for(
                asyncio.gather(_read_bounded(process.stdout), _read_bounded(process.stderr), process.wait()),
                timeout=timeout_seconds,
            )
            if process.returncode != 0:
                missing = re.search(rb"ModuleNotFoundError: No module named '([a-zA-Z0-9_.]+)'", errors)
                if missing:
                    module = missing.group(1).decode("ascii")
                    raise SandboxConfigurationError(f"沙箱镜像缺少依赖 {module}，请在 Tool 中声明对应安装包并保存、准备环境后重新试运行")
                if b"No such image" in errors or b"pull access denied" in errors:
                    raise SandboxConfigurationError("沙箱镜像不可用，请先构建镜像或在离线环境导入镜像")
                raise ValueError("脚本执行失败")
            try:
                result = json.loads(output)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("脚本必须输出一个 JSON 值") from exc
            return json.dumps(result, ensure_ascii=False)
        except BaseException:
            await _remove_container(container_name)
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
