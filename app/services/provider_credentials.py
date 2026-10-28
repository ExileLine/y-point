# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:46:07
# @Author  : yangyuexiong
# @File    : provider_credentials.py

import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_config
from app.core.custom_exception import CustomException


def _cipher() -> Fernet:
    """读取独立加密密钥并构造凭据加解密器。"""
    key = get_config().CREDENTIAL_ENCRYPTION_KEY
    if not key:
        raise CustomException(status_code=503, custom_code=10005, detail="未配置供应商凭据加密密钥")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise CustomException(status_code=503, custom_code=10005, detail="供应商凭据加密密钥无效") from None


def encrypt_credentials(api_key: str | None, custom_headers: dict[str, str]) -> str | None:
    """加密 API 密钥与自定义请求头，空凭据不占用密文列。"""
    if not api_key and not custom_headers:
        return None
    payload = json.dumps(
        {
            "api_key": api_key,
            "custom_headers": custom_headers,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _cipher().encrypt(payload.encode()).decode()


def decrypt_credentials(ciphertext: str | None) -> tuple[str | None, dict[str, str]]:
    """解密供应商凭据，密钥错误或数据损坏时给出非敏感错误。"""
    if not ciphertext:
        return None, {}
    try:
        payload = json.loads(_cipher().decrypt(ciphertext.encode()))
        api_key = payload["api_key"]
        headers = payload["custom_headers"]
        if (api_key is not None and not isinstance(api_key, str)) or not isinstance(headers, dict):
            raise ValueError("invalid credential shape")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
            raise ValueError("invalid header shape")
        return api_key, headers
    except (InvalidToken, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise CustomException(status_code=503, custom_code=10005, detail="供应商凭据无法解密") from None
