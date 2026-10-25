# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : base.py

import json
import re
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import pytz
from sqlalchemy import BigInteger, DateTime, Integer, event
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


TZ = pytz.timezone("Asia/Shanghai")


def camel_to_snake(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def shanghai_now(*, naive: bool = False) -> datetime:
    dt = datetime.now(TZ)
    if naive:
        return dt.replace(tzinfo=None)
    return dt


def shanghai_datetime(value: Optional[datetime], *, naive: bool = False) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        localized_value = TZ.localize(value)
    else:
        localized_value = value.astimezone(TZ)
    if naive:
        return localized_value.replace(tzinfo=None)
    return localized_value


class ShanghaiDateTime(TypeDecorator[datetime]):
    """将应用中的上海本地时间与各数据库的时间列相互转换。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        return shanghai_datetime(value, naive=dialect.name != "postgresql")

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return shanghai_datetime(value, naive=True)

TABLE_PREFIX = "yp_"  # 表名称前缀

class CustomBaseModel(Base):
    __abstract__ = True
    _json_string_fields: set[str] = set()
    __table_prefix__ = TABLE_PREFIX
    __table_name__: str | None = None
    __enable_auto_id__ = True
    __enable_audit_columns__ = True

    @declared_attr.directive
    def __tablename__(cls) -> str:
        suffix = cls.__dict__.get("__table_name__") or camel_to_snake(cls.__name__)
        prefix = getattr(cls, "__table_prefix__", TABLE_PREFIX)
        if suffix.startswith(prefix):
            return suffix
        return f"{prefix}{suffix}"

    @declared_attr
    def id(cls) -> Mapped[int]:
        if not getattr(cls, "__enable_auto_id__", True):
            return None
        return mapped_column(Integer, primary_key=True, autoincrement=True, comment="ID", sort_order=-1000)

    @declared_attr
    def create_time(cls) -> Mapped[datetime]:
        if not getattr(cls, "__enable_audit_columns__", True):
            return None
        return mapped_column(
            ShanghaiDateTime(),
            nullable=False,
            default=lambda: shanghai_now(naive=True),
            comment="创建时间",
            sort_order=1000,
        )

    @declared_attr
    def update_time(cls) -> Mapped[datetime]:
        if not getattr(cls, "__enable_audit_columns__", True):
            return None
        return mapped_column(
            ShanghaiDateTime(),
            nullable=False,
            default=lambda: shanghai_now(naive=True),
            onupdate=lambda: shanghai_now(naive=True),
            comment="更新时间",
            sort_order=1001,
        )

    @declared_attr
    def create_timestamp(cls) -> Mapped[int]:
        if not getattr(cls, "__enable_audit_columns__", True):
            return None
        return mapped_column(BigInteger, nullable=False, default=lambda: int(time.time()), comment="创建时间戳", sort_order=1002)

    @declared_attr
    def update_timestamp(cls) -> Mapped[int]:
        if not getattr(cls, "__enable_audit_columns__", True):
            return None
        return mapped_column(
            BigInteger,
            nullable=False,
            default=lambda: int(time.time()),
            onupdate=lambda: int(time.time()),
            comment="更新时间戳",
            sort_order=1003,
        )

    @declared_attr
    def is_deleted(cls) -> Mapped[int]:
        if not getattr(cls, "__enable_audit_columns__", True):
            return None
        return mapped_column(BigInteger, nullable=True, default=0, comment="逻辑删除标识", sort_order=1004)

    @declared_attr
    def status(cls) -> Mapped[int]:
        if not getattr(cls, "__enable_audit_columns__", True):
            return None
        return mapped_column(BigInteger, nullable=True, default=1, comment="状态(通用字段)", sort_order=1005)

    def touch(self):
        mapper_columns = self.__mapper__.columns
        now = shanghai_now(naive=True)
        if "update_time" in mapper_columns:
            setattr(self, "update_time", now)
        if "update_timestamp" in mapper_columns:
            setattr(self, "update_timestamp", int(time.time()))

    def _serialize_value(self, key: str, value: Any) -> Any:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, datetime):
            localized_value = shanghai_datetime(value)
            if localized_value is not None:
                return localized_value.strftime("%Y-%m-%d %H:%M:%S")
            return value
        if isinstance(value, Decimal):
            return float(value)
        if key in self._json_string_fields and isinstance(value, str):
            v = value.strip()
            if (v.startswith("{") and v.endswith("}")) or (v.startswith("[") and v.endswith("]")):
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    return value
        return value

    def to_dict(self, exclude: Optional[set[str]] = None, include: Optional[set[str]] = None) -> dict[str, Any]:
        exclude = exclude or set()
        data = {}
        for column in self.__mapper__.columns:
            key = column.key
            if include is not None and key not in include:
                continue
            if key in exclude:
                continue
            data[key] = self._serialize_value(key, getattr(self, key, None))
        return data


@event.listens_for(CustomBaseModel, "before_insert", propagate=True)
def _before_insert_set_audit_timezone(_mapper, _connection, target: CustomBaseModel) -> None:
    mapper_columns = target.__mapper__.columns
    now = shanghai_now(naive=True)
    now_ts = int(time.time())
    if "create_time" in mapper_columns:
        create_time = shanghai_datetime(getattr(target, "create_time", None), naive=True) or now
        setattr(target, "create_time", create_time)
    if "update_time" in mapper_columns:
        update_time = shanghai_datetime(getattr(target, "update_time", None), naive=True) or now
        setattr(target, "update_time", update_time)
    if "create_timestamp" in mapper_columns and getattr(target, "create_timestamp", None) is None:
        setattr(target, "create_timestamp", now_ts)
    if "update_timestamp" in mapper_columns and getattr(target, "update_timestamp", None) is None:
        setattr(target, "update_timestamp", now_ts)


@event.listens_for(CustomBaseModel, "before_update", propagate=True)
def _before_update_set_audit_timezone(_mapper, _connection, target: CustomBaseModel) -> None:
    mapper_columns = target.__mapper__.columns
    if "update_time" in mapper_columns:
        update_time = shanghai_datetime(getattr(target, "update_time", None), naive=True) or shanghai_now(naive=True)
        setattr(target, "update_time", update_time)
    if "update_timestamp" in mapper_columns:
        setattr(target, "update_timestamp", int(time.time()))
