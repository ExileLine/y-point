# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : pagination.py

from typing import Dict, List

from pydantic import BaseModel
from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.pagination import page_size, query_result
from app.utils.time_tools import TimeTools


class JsonFieldHandle:
    def __init__(self):
        """初始化 JSON 字段处理占位对象。"""
        pass


class CommonPaginateQuery:
    def __init__(
        self,
        request_data: BaseModel,
        orm_model,
        db_session: AsyncSession,
        model_pydantic: BaseModel = None,
        like_list: list = None,
        where_list: list = None,
        order_by_list: list = None,
        filter_range: dict = None,
        json_field_keys_to_obj: List[Dict] = None,
        json_field_obj_to_keys: List[Dict] = None,
        output_model: BaseModel = None,
        exclude_field: set = None,
        skip_list: List = None,
    ):
        """保存分页请求、模型、会话及筛选排序配置。"""
        self.request_data = request_data
        self.orm_model = orm_model
        self.db_session = db_session
        self.model_pydantic = model_pydantic
        self.like_list = like_list
        self.where_list = where_list
        self.order_by_list = order_by_list
        self.json_field_keys_to_obj = json_field_keys_to_obj
        self.json_field_obj_to_keys = json_field_obj_to_keys
        self.filter_range = filter_range
        self.output_model = output_model
        self.exclude_field = exclude_field or set()

        self.like_conditions = []
        self.where_conditions = []
        self.range_conditions = []
        self.records = []
        self.normal_data = []

        if skip_list:
            self.skip_list = skip_list + ["is_deleted"]
        else:
            self.skip_list = ["is_deleted"]

    @staticmethod
    def _parse_filter_key(key: str) -> tuple[str, str]:
        """将筛选键拆分为字段名和操作符，缺省使用等值比较。"""
        if "__" in key:
            field, op = key.split("__", 1)
            return field, op
        return key, "eq"

    def _build_expr(self, key: str, value):
        """将筛选字段和值转换为 SQL 表达式，不支持的字段或操作返回空值。"""
        field, op = self._parse_filter_key(key)
        column = getattr(self.orm_model, field, None)
        if column is None:
            return None

        if op == "eq":
            return column == value
        if op == "icontains":
            return column.ilike(f"%{value}%")
        if op == "gte":
            return column >= value
        if op == "lte":
            return column <= value
        if op == "gt":
            return column > value
        if op == "lt":
            return column < value
        if op == "in":
            if not isinstance(value, (list, tuple, set)):
                value = [value]
            return column.in_(value)
        if op == "isnull":
            return column.is_(None) if bool(value) else column.is_not(None)
        return None

    async def build_filter_conditions(self):
        """从请求字段收集模糊查询和等值查询条件。"""
        for k, v in self.request_data.model_dump().items():
            if v is None or (bool(v) is False and k not in self.skip_list):
                continue
            if k in self.like_list:
                self.like_conditions.append(getattr(self.orm_model, k).ilike(f"%{v}%"))
            if k in self.where_list:
                self.where_conditions.append(getattr(self.orm_model, k) == v)

    async def build_like(self):
        """将缺省的模糊查询字段初始化为空列表。"""
        if not self.like_list:
            self.like_list = []

    async def build_where(self):
        """将缺省的等值查询字段初始化为空列表。"""
        if not self.where_list:
            self.where_list = []

    async def build_range(self):
        """将范围筛选配置转换为有效的 SQL 查询条件。"""
        if not self.filter_range:
            return
        for k, v in self.filter_range.items():
            if v == "":
                continue
            expr = self._build_expr(k, v)
            if expr is not None:
                self.range_conditions.append(expr)

    async def build_order_by(self):
        """将缺省的排序字段初始化为空列表。"""
        if not self.order_by_list:
            self.order_by_list = []

    def _build_order_clauses(self):
        """解析排序字段和降序前缀，返回有效的排序表达式。"""
        clauses = []
        for field in self.order_by_list:
            if field.startswith("-"):
                key = field[1:]
                column = getattr(self.orm_model, key, None)
                if column is not None:
                    clauses.append(desc(column))
            else:
                column = getattr(self.orm_model, field, None)
                if column is not None:
                    clauses.append(asc(column))
        return clauses

    async def _handle_json_field_keys_to_obj(self, model_data: dict):
        """按配置将记录中的 JSON 关联键转换为关联对象。"""
        if not self.json_field_keys_to_obj:
            return
        for d in self.json_field_keys_to_obj:
            d_field = d.get("field")
            d_model = d.get("model")
            d_query_key = d.get("query_key")
            d_exclude_field = d.get("exclude_field") or set()
            value = model_data.get(d_field)
            if not (d_field and d_model and d_query_key and isinstance(value, list) and value):
                continue

            query_col = getattr(d_model, d_query_key, None)
            if query_col is None:
                continue
            stmt = select(d_model).where(query_col.in_(value))
            result = await self.db_session.execute(stmt)
            d_model_list = result.scalars().all()
            if not d_model_list:
                continue
            model_data[d_field] = [m.to_dict(exclude=d_exclude_field) for m in d_model_list]

    async def build_query(self):
        """执行筛选、排序和分页查询，并返回记录与总数。"""
        await self.build_like()
        await self.build_where()
        await self.build_filter_conditions()
        await self.build_range()
        await self.build_order_by()

        page = self.request_data.page
        now_page = page
        size = self.request_data.size
        offset, limit = page_size(page, size)

        where_clauses = []
        if self.where_conditions:
            where_clauses.extend(self.where_conditions)
        if self.like_conditions:
            where_clauses.append(or_(*self.like_conditions))
        if self.range_conditions:
            where_clauses.extend(self.range_conditions)

        count_stmt = select(func.count()).select_from(self.orm_model)
        if where_clauses:
            count_stmt = count_stmt.where(and_(*where_clauses))
        total_count = (await self.db_session.execute(count_stmt)).scalar_one()

        stmt = select(self.orm_model)
        if where_clauses:
            stmt = stmt.where(and_(*where_clauses))
        order_clauses = self._build_order_clauses()
        if order_clauses:
            stmt = stmt.order_by(*order_clauses)
        stmt = stmt.offset(offset).limit(limit)

        model_list = (await self.db_session.execute(stmt)).scalars().all()

        records = []
        for model in model_list:
            model_data = model.to_dict(exclude=self.exclude_field)
            if model_data.get("create_time"):
                model_data["create_time"] = TimeTools.convert_to_standard_format(model_data["create_time"])
            if model_data.get("update_time"):
                model_data["update_time"] = TimeTools.convert_to_standard_format(model_data["update_time"])
            if model_data.get("go_live_time"):
                model_data["go_live_time"] = TimeTools.convert_to_standard_format(model_data["go_live_time"])

            await self._handle_json_field_keys_to_obj(model_data)
            records.append(model_data)

        if self.output_model:
            records = [self.output_model.model_validate(model).model_dump() for model in records]

        self.records = records
        self.normal_data = query_result(records=self.records, now_page=now_page, total=total_count)
        return self.normal_data
