# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : script_sandbox.py

"""通过 Docker 运行仅接收 JSON 输入的用户脚本。"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

MAX_OUTPUT_BYTES = 64 * 1024
IMAGE = "python:3.12-slim-bookworm"
SANDBOX_ROOT = Path(__file__).resolve().parents[2] / ".run-private" / "tool-sandbox"


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


async def run_script(language: str, source: str, arguments: dict, timeout_seconds: int) -> str:
    """在无网络、只读、非特权容器中执行脚本并返回有界 JSON 结果。"""
    if language not in {"python", "bash"} or timeout_seconds <= 0:
        raise ValueError("脚本运行配置无效")
    input_bytes = json.dumps(arguments, ensure_ascii=False).encode()
    if len(input_bytes) > 64 * 1024:
        raise ValueError("脚本输入超过 64 KiB")
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = "main.py" if language == "python" else "main.sh"
    container_name = f"yp-tool-{uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="run-", dir=SANDBOX_ROOT) as directory:
        script = Path(directory) / filename
        script.write_text(source, encoding="utf-8")
        os.chmod(script, 0o644)
        interpreter = ["python", "-B", f"/app/{filename}"] if language == "python" else ["bash", f"/app/{filename}"]
        command = ["timeout", "-s", "KILL", str(timeout_seconds + 2), *interpreter]
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--name",
            container_name,
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
            IMAGE,
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
            output, _, _ = await asyncio.wait_for(
                asyncio.gather(_read_bounded(process.stdout), _read_bounded(process.stderr), process.wait()),
                timeout=timeout_seconds,
            )
            if process.returncode != 0:
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
