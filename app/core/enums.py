# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : enums.py

from enum import Enum


class UserStatus(str, Enum):
    normal = "正常"
    disable = "禁用"