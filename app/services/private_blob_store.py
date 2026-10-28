# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 22:48:04
# @Author  : yangyuexiong
# @File    : private_blob_store.py

"""以随机键访问本地私有目录或私有 S3 兼容对象存储。"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import boto3
from botocore.config import Config

from app.core.config import get_config


def _check_key(key: str) -> None:
    """存储键只能是服务端生成的 32 位小写十六进制值。"""
    if len(key) != 32 or any(char not in "0123456789abcdef" for char in key):
        raise ValueError("文件存储键无效")


def _object_key(key: str) -> str:
    """将随机键限制在本服务的对象前缀下。"""
    _check_key(key)
    prefix = get_config().ARTIFACT_S3_PREFIX
    if not prefix or not prefix.endswith("/") or prefix.startswith("/") or ".." in prefix.split("/"):
        raise ValueError("对象存储前缀无效")
    return prefix + key


def using_s3() -> bool:
    """显式选择对象存储，拒绝未知存储模式。"""
    backend = get_config().ARTIFACT_STORAGE_BACKEND
    if backend not in {"local", "s3"}:
        raise ValueError("文件存储后端无效")
    return backend == "s3"


def _client():
    """使用显式密钥或部署环境的 IAM 凭据链创建 S3 客户端。"""
    config = get_config()
    if not config.ARTIFACT_S3_BUCKET:
        raise ValueError("未配置私有对象存储桶")
    if bool(config.ARTIFACT_S3_ACCESS_KEY_ID) != bool(config.ARTIFACT_S3_SECRET_ACCESS_KEY):
        raise ValueError("对象存储访问密钥必须成对配置")
    options = {
        "endpoint_url": config.ARTIFACT_S3_ENDPOINT_URL or None,
        "region_name": config.ARTIFACT_S3_REGION or None,
        "config": Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 1}),
    }
    if config.ARTIFACT_S3_ACCESS_KEY_ID:
        options["aws_access_key_id"] = config.ARTIFACT_S3_ACCESS_KEY_ID
        options["aws_secret_access_key"] = config.ARTIFACT_S3_SECRET_ACCESS_KEY
    return boto3.client("s3", **options)


async def store_blob(root: Path, key: str, path: Path) -> None:
    """写入私有对象；本地文件路径由调用方在事务失败时清理。"""
    if not using_s3():
        return
    config = get_config()
    await asyncio.to_thread(
        _client().upload_file, str(path), config.ARTIFACT_S3_BUCKET, _object_key(key), ExtraArgs={"ServerSideEncryption": "AES256"}
    )


async def read_blob(root: Path, key: str, *, max_bytes: int) -> bytes:
    """有界读取私有对象；调用方先完成所有权和状态检查。"""
    _check_key(key)
    if not using_s3():
        path = root / key
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("文件不存在")
        with path.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    else:
        config = get_config()

        def fetch() -> bytes:
            """有界读取 S3 对象正文，并确保关闭远端响应流。"""
            response = _client().get_object(Bucket=config.ARTIFACT_S3_BUCKET, Key=_object_key(key))
            body = response["Body"]
            try:
                return body.read(max_bytes + 1)
            finally:
                body.close()

        content = await asyncio.to_thread(fetch)
    if len(content) > max_bytes:
        raise ValueError("文件超过读取限制")
    return content


async def delete_blob(root: Path, key: str) -> None:
    """删除随机键对应的私有文件或对象。"""
    _check_key(key)
    if not using_s3():
        (root / key).unlink(missing_ok=True)
        return
    config = get_config()
    await asyncio.to_thread(_client().delete_object, Bucket=config.ARTIFACT_S3_BUCKET, Key=_object_key(key))


async def old_blob_batches(*, before: datetime, batch_size: int) -> AsyncIterator[list[str]]:
    """分页列出本服务前缀下的旧对象，不将全桶键载入内存。"""
    if not using_s3():
        return
    config = get_config()
    prefix = _object_key("0" * 32)[:-32]
    token = None
    while True:
        options = {"Bucket": config.ARTIFACT_S3_BUCKET, "Prefix": prefix, "MaxKeys": batch_size}
        if token is not None:
            options["ContinuationToken"] = token
        response = await asyncio.to_thread(_client().list_objects_v2, **options)
        keys = []
        for item in response.get("Contents", []):
            if not item["Key"].startswith(prefix) or item["LastModified"].timestamp() >= before.timestamp():
                continue
            key = item["Key"][len(prefix) :]
            if len(key) == 32 and all(char in "0123456789abcdef" for char in key):
                keys.append(key)
        yield keys
        if not response.get("IsTruncated"):
            return
        token = response.get("NextContinuationToken")
        if not token:
            raise RuntimeError("对象存储分页缺少继续标记")
