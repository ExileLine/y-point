# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : celery_app.py

from celery import Celery

from app.core.config import get_config

project_config = get_config()

celery_app = Celery(
    "y-point-service",
    broker=project_config.celery_broker_url,
    backend=project_config.celery_result_backend,
    include=[],
)

celery_app.conf.update(
    task_default_queue=project_config.CELERY_TASK_QUEUE,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=False,
)