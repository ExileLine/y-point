# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:13:39
# @Author  : yangyuexiong
# @File    : mcp_stdio.py

"""在固定镜像容器中运行 MCP stdio，限制消息和日志并回收进程。"""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from uuid import uuid4

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from app.core.config import get_config
from app.services.mcp_connections import stdio_profiles
from app.services.script_sandbox import SANDBOX_ROOT
from app.services.tool_resources import managed_workspace, resource_labels


async def _remove_container(name: str) -> None:
    """限时强制删除容器，Docker 客户端卡住时也回收其进程。"""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _spawn_stdio(command: list[str], limit: int):
    """启动单个容器客户端并建立有界管道，测试可替换为受控子进程。"""
    return await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=limit + 1,
    )


@asynccontextmanager
async def stdio_connection(config: dict, env: dict[str, str], timeout: int):
    """用有界 JSON-RPC 管道连接容器，禁止自动拉镜像、网络及业务目录挂载。"""
    profile = stdio_profiles().get(config.get("stdio_profile"))
    if profile is None or any(config.get(key) != profile.get(key, []) for key in ("image", "command", "env_keys")):
        raise ValueError("stdio 启动配置已变化，请重新发布连接和工具")
    if config["args"] != [*profile.get("args", []), *config.get("user_args", [])] or not set(env) <= set(profile.get("env_keys", [])):
        raise ValueError("stdio 执行配置或凭据白名单已变化，请重新发布")
    failure: list[str] = []
    limit = get_config().MCP_RESPONSE_MAX_BYTES
    name = "yp-mcp-" + uuid4().hex
    receive_writer, receive = anyio.create_memory_object_stream(0)
    send, send_reader = anyio.create_memory_object_stream(0)
    with managed_workspace(SANDBOX_ROOT, "mcp-", timeout) as directory:
        env_path = Path(directory) / "mcp.env"
        env_path.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
        os.chmod(env_path, 0o600)
        command = [
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "--interactive",
            "--init",
            "--log-driver=none",
            "--name",
            name,
            *resource_labels(timeout),
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=1",
            "--user=65534:65534",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=16m",
            "--env-file",
            str(env_path),
            "--entrypoint",
            config["command"][0],
            config["image"],
            *config["command"][1:],
            *config["args"],
        ]
        try:
            process = await _spawn_stdio(command, limit)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await _remove_container(name)
                await receive.aclose()
                await send.aclose()
                await receive_writer.aclose()
                await send_reader.aclose()
            raise

        async def report_failure(message: str) -> None:
            """只向 SDK 返回受控错误，不把原始协议、环境变量或日志写入公开结果。"""
            if not failure:
                failure.append(message)
            await receive_writer.aclose()

        async def read_messages() -> None:
            """在 SDK 解析前限制单行及累计字节，拒绝非协议标准输出。"""
            received = 0
            try:
                async with receive_writer:
                    while line := await process.stdout.readline():
                        received += len(line)
                        if received > limit or not line.endswith(b"\n"):
                            raise ValueError("stdio 输出超限或消息未按行分隔")
                        try:
                            message = JSONRPCMessage.model_validate_json(line)
                        except Exception:
                            raise ValueError("stdio 标准输出不是有效的 MCP 消息") from None
                        await receive_writer.send(SessionMessage(message))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass
            except Exception:
                await report_failure("MCP stdio 消息无效或超过接收上限")

        async def write_messages() -> None:
            """按 MCP 换行分帧写入，防止超大请求和无法结束的管道写入。"""
            async with send_reader:
                async for message in send_reader:
                    data = message.message.model_dump_json(by_alias=True, exclude_none=True).encode() + b"\n"
                    if len(data) > limit:
                        await report_failure("MCP stdio 请求超过字节上限")
                        return
                    try:
                        process.stdin.write(data)
                        await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        await report_failure("MCP stdio 进程已退出")
                        return

        async def drain_logs() -> None:
            """丢弃有界 stderr，不阻塞子进程，也不泄露其日志中的凭据。"""
            received = 0
            while chunk := await process.stderr.read(4096):
                received += len(chunk)
                if received > limit:
                    await report_failure("MCP stdio 日志超过字节上限")
                    return

        tasks = [asyncio.create_task(read_messages()), asyncio.create_task(write_messages()), asyncio.create_task(drain_logs())]

        async def cleanup() -> None:
            """在独立任务中完成资源清理，不被外层总时限打断。"""
            process.stdin.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            async def discard(stream) -> None:
                """清理时继续排空管道，避免缓冲区暂停读取阻塞子进程退出。"""
                while await stream.read(4096):
                    pass

            drains = [asyncio.create_task(discard(process.stdout)), asyncio.create_task(discard(process.stderr))]
            try:
                await asyncio.wait_for(process.wait(), timeout=0.2)
            except TimeoutError:
                pass
            await _remove_container(name)
            if process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.gather(*drains, return_exceptions=True)
            await receive.aclose()
            await send.aclose()
            await receive_writer.aclose()
            await send_reader.aclose()

        try:
            yield receive, send, None
            if failure:
                raise ValueError(failure[0])
        finally:
            # 正常结束先关闭 stdin；超时、取消和异常同样回收容器及后台管道任务。
            with anyio.CancelScope(shield=True):
                closing = asyncio.create_task(cleanup())
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    await closing
                    raise
