# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : time_tools.py

from datetime import datetime

from dateutil import parser


class TimeTools:
    @staticmethod
    def timestamp_to_datetime(timestamp) -> str:
        dt_object = datetime.fromtimestamp(timestamp)
        str_date = dt_object.strftime("%Y-%m-%d %H:%M:%S")
        return str_date

    @staticmethod
    def datetime_to_timestamp(date_string, set_cn: bool = False, is_ms: bool = False):
        if not date_string:
            return 0

        datetime_object = datetime.strptime(date_string, "%Y-%m-%d %H:%M:%S")
        timestamp = datetime_object.timestamp()
        if set_cn:
            handle_timestamp = int(timestamp) + 28800
        else:
            handle_timestamp = int(timestamp)

        if is_ms:
            return handle_timestamp * 1000
        return handle_timestamp

    @staticmethod
    def today_zero_timestamp() -> int:
        today = datetime.now().date()
        midnight = datetime.combine(today, datetime.min.time())
        timestamp = int(midnight.timestamp())
        return timestamp

    @staticmethod
    def convert_to_standard_format(datetime_str) -> str:
        try:
            if isinstance(datetime_str, datetime):
                return datetime_str.strftime("%Y-%m-%d %H:%M:%S")
            dt = parser.parse(datetime_str)
            formatted_dt = dt.strftime("%Y-%m-%d %H:%M:%S")
            return formatted_dt
        except BaseException:
            return datetime_str