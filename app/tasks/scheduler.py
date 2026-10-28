# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : scheduler.py

import pytz
from datetime import datetime
from enum import Enum

from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.aps_task import ApsTask
from app.tasks import tasks as TaskDict

scheduler = AsyncIOScheduler()


class TriggerType(str, Enum):
    date = "date"
    interval = "interval"
    cron = "cron"


class TriggerHandler:
    def __init__(
        self,
        task_id: str,
        trigger_type: TriggerType,
        trigger_time: str = None,
        interval_kw: dict = None,
        cron_expression: str = None,
        timezone=pytz.timezone("Asia/Shanghai"),
        task_function_name: str = None,
        skip_function_check: bool = False,
        task_function=None,
        task_function_args: list = None,
        task_function_kwargs: dict = None,
    ):
        """保存任务标识、触发配置、执行函数和调用参数。"""
        self.task_id = task_id
        self.trigger_type = trigger_type
        self.trigger = None
        self.trigger_param = {}
        self.trigger_time = trigger_time
        self.interval_kw = interval_kw
        self.cron_expression = cron_expression
        self.timezone = timezone
        self.task_function_name = task_function_name
        self.skip_function_check = skip_function_check
        self.task_function = task_function
        self.task_function_args = task_function_args
        self.task_function_kwargs = task_function_kwargs

    def date_trigger(self):
        """校验一次性执行时间并构建日期触发器。"""
        if not self.trigger_time:
            raise TypeError("DateTrigger 缺少 trigger_time")

        run_date = datetime.strptime(self.trigger_time, "%Y-%m-%d %H:%M:%S")
        trigger = DateTrigger(run_date=run_date, timezone=self.timezone)
        self.trigger_param = {
            "trigger_time": run_date,
        }
        return trigger

    def interval_trigger(self):
        """校验间隔参数并构建周期触发器。"""
        if not isinstance(self.interval_kw, dict):
            raise TypeError("IntervalTrigger 参数 interval_kw 错误")

        trigger = IntervalTrigger(timezone=self.timezone, **self.interval_kw)
        self.trigger_param = {
            "interval_kw": self.interval_kw,
        }
        return trigger

    def cron_trigger(self):
        """校验 Cron 表达式并构建定时触发器。"""
        if not self.cron_expression:
            raise TypeError("CronTrigger 缺少 cron_expression")

        trigger = CronTrigger.from_crontab(expr=self.cron_expression, timezone=self.timezone)
        self.trigger_param = {
            "cron_expression": self.cron_expression,
        }
        return trigger

    def get_trigger(self):
        """按配置的触发类型构建并保存任务触发器。"""
        trigger_dict = {
            "date": self.date_trigger,
            "interval": self.interval_trigger,
            "cron": self.cron_trigger,
        }
        self.trigger = trigger_dict.get(self.trigger_type)()
        return self.trigger

    def get_task_function(self):
        """返回显式传入或按名称查找的任务函数。"""
        if self.skip_function_check:
            return self.task_function

        if hasattr(TaskDict, self.task_function_name):
            self.task_function = getattr(TaskDict, self.task_function_name)
            return self.task_function
        raise AttributeError(f"任务函数 '{self.task_function_name}' 不存在")


class TaskHandler(TriggerHandler):
    def add_task(self):
        """注册任务并返回成功状态及中文结果消息。"""
        self.get_trigger()
        self.get_task_function()

        try:
            scheduler.add_job(
                self.task_function, trigger=self.trigger, id=self.task_id, args=self.task_function_args, kwargs=self.task_function_kwargs
            )
            return True, f"定时任务: {self.task_id} 新增成功"
        except ConflictingIdError as e:
            return False, f"定时任务: {self.task_id} 已存在: {e}"

    def update_task(self):
        """删除旧任务后重新注册，并返回更新结果。"""
        remove_result, remove_message = self.remove_task(task_id=self.task_id)
        add_result, add_message = self.add_task()
        del remove_message, add_message

        if not remove_result or not add_result:
            return False, f"定时任务: {self.task_id} 更新失败"
        return True, f"定时任务: {self.task_id} 更新成功"

    @classmethod
    def remove_task(cls, task_id):
        """按任务 ID 删除已注册任务并返回结果。"""
        if scheduler.get_job(task_id):
            scheduler.remove_job(task_id)
            return True, f"定时任务: {task_id} 删除成功"
        return False, f"定时任务: {task_id} 不存在"

    @classmethod
    def get_task_state(cls, task_id):
        """返回指定任务的 ID 与下次执行时间，不存在时返回空字典。"""
        job = scheduler.get_job(task_id)
        if job:
            res = {
                "id": job.id,
                "next_run_time": job.next_run_time,
            }
            return res
        return {}

    @staticmethod
    def get_all_task_states():
        """返回全部已注册任务的 ID 与下次执行时间。"""
        all_jobs = scheduler.get_jobs()
        task_states = []
        for job in all_jobs:
            d = {
                "id": job.id,
                "next_run_time": job.next_run_time,
            }
            task_states.append(d)
        return task_states


async def scheduler_init():
    """加载数据库任务及示例周期任务，然后启动调度器。"""
    async with AsyncSessionLocal() as db:
        stmt = select(ApsTask).where(ApsTask.is_deleted == 0)
        tasks = (await db.execute(stmt)).scalars().all()

    for task in tasks:
        task_id = task.task_id
        trigger_type = task.trigger_type
        trigger_param: dict = task.trigger_param
        task_function_name = task.task_function_name
        task_function_args = task.task_function_args
        task_function_kwargs = task.task_function_kwargs

        if trigger_param:
            task_handler = TaskHandler(
                task_id=task_id,
                trigger_type=trigger_type,
                task_function_name=task_function_name,
                task_function_args=task_function_args,
                task_function_kwargs=task_function_kwargs,
            )

            for k, v in trigger_param.items():
                setattr(task_handler, k, v)

            task_handler.add_task()

    test1 = TaskHandler(
        task_id=TaskDict.test_sync_task.__name__,
        trigger_type=TriggerType.interval,
        interval_kw={
            "weeks": 0,
            "days": 0,
            "hours": 0,
            "minutes": 0,
            "seconds": 5,
            "start_date": None,
            "end_date": None,
        },
        task_function_name=TaskDict.test_sync_task.__name__,
    )
    test1.add_task()

    test2 = TaskHandler(
        task_id=TaskDict.test_async_task.__name__,
        trigger_type=TriggerType.interval,
        interval_kw={
            "weeks": 0,
            "days": 0,
            "hours": 0,
            "minutes": 0,
            "seconds": 5,
            "start_date": None,
            "end_date": None,
        },
        task_function_name=TaskDict.test_async_task.__name__,
    )
    test2.add_task()

    scheduler.start()
