# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 15:17:57
# @Author  : yangyuexiong
# @File    : preflight.py

"""启动容器前校验生产密钥及必要的私有目录，不输出凭据。"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.fernet import Fernet  # noqa: E402

from app.core.config import get_config  # noqa: E402


def main() -> None:
    """
    拒绝不完整的部署配置，并准备运行用户拥有的目录。
    检查项要求
    运行环境：使用 production 或 staging，并关闭 DEBUG
    JWT密钥：长度至少 32 个字符，拒绝指定的示例值
    供应商凭据加密密钥：符合 Fernet 密钥格式
    持久化目录：使用绝对路径，目录能够创建并且可写
    Docker沙箱：当部署要求启用沙箱时，检查引擎连接、沙箱镜像和依赖代理脚本
    :return:
    """
    config = get_config()
    if config.DEBUG or config.ENV_NAME not in {"production", "staging"}:
        raise SystemExit("部署容器要求 DEBUG=False，ENV 为 production 或 staging")
    if len(config.SECRET_KEY) < 32 or config.SECRET_KEY in {"change-me", "your-secret-key"}:
        raise SystemExit("部署必须配置独立的至少 32 字符 SECRET_KEY")
    try:
        Fernet(config.CREDENTIAL_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        raise SystemExit("部署必须配置有效的 CREDENTIAL_ENCRYPTION_KEY") from None
    for value in (config.ARTIFACT_STORAGE_DIR, config.SCRIPT_DEPENDENCY_STORAGE_DIR, config.SCRIPT_SANDBOX_STORAGE_DIR):
        path = Path(value)
        if not path.is_absolute():
            raise SystemExit("容器部署的持久化目录必须使用绝对路径")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not os.access(path, os.W_OK):
            raise SystemExit("持久化目录不可写，请检查部署 UID/GID 和挂载权限")
    if os.getenv("YPOINT_SANDBOX_REQUIRED") == "1":
        for command in (["docker", "version"], ["docker", "image", "inspect", config.SCRIPT_SANDBOX_IMAGE]):
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            if result.returncode:
                raise SystemExit("Docker 沙箱不可用：检查 socket 权限及 SCRIPT_SANDBOX_IMAGE 是否已构建")
        if not Path(config.SCRIPT_PACKAGE_PROXY_FILE).is_file():
            raise SystemExit("依赖代理脚本不存在，请重新执行部署 init 同步受信任脚本")


if __name__ == "__main__":
    main()
