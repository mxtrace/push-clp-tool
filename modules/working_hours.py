"""
working_hours.py — 工作时间计算模块
工作时间定义：周一–周五 09:00–18:00 北京时间（UTC+8），剔除中国法定节假日
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytz
from chinese_calendar import is_workday as _cn_is_workday

BEIJING_TZ = pytz.timezone("Asia/Shanghai")
_WORK_START = 9   # 09:00
_WORK_END   = 18  # 18:00


def _bj(dt: datetime) -> datetime:
    """将 datetime 转换为北京时区（无 tzinfo 时视为北京时间本地化）。"""
    if dt.tzinfo is None:
        return BEIJING_TZ.localize(dt)
    return dt.astimezone(BEIJING_TZ)


def is_workday(d: date) -> bool:
    """判断是否为工作日（含调休、剔除法定假日）。"""
    return _cn_is_workday(d)


def calc_working_hours(start: datetime, end: datetime) -> float:
    """
    计算两个时间点之间的工作小时数。
    工作时间：周一–周五 09:00–18:00 北京时间，中国法定节假日除外。

    Args:
        start: 起始时间（无 tzinfo 视为北京时间）
        end:   结束时间（无 tzinfo 视为北京时间）

    Returns:
        float: 工作小时数（精确到秒级）
    """
    start_bj = _bj(start)
    end_bj   = _bj(end)

    if end_bj <= start_bj:
        return 0.0

    total = timedelta()
    cursor = start_bj

    while cursor.date() <= end_bj.date():
        d = cursor.date()
        if is_workday(d):
            day_start = BEIJING_TZ.localize(datetime(d.year, d.month, d.day, _WORK_START))
            day_end   = BEIJING_TZ.localize(datetime(d.year, d.month, d.day, _WORK_END))
            seg_start = max(cursor, day_start)
            seg_end   = min(end_bj, day_end)
            if seg_end > seg_start:
                total += seg_end - seg_start
        # 推进到次日 00:00
        cursor = BEIJING_TZ.localize(datetime(d.year, d.month, d.day)) + timedelta(days=1)

    return total.total_seconds() / 3600.0


def hours_until(target: datetime, now: datetime | None = None) -> float:
    """
    计算从 now 到 target 的工作小时数。
    如果 target 已过，返回负数（表示已超时）。

    用于 PRD 步骤 F 计算 hours_to_cutoff。
    """
    if now is None:
        now = datetime.now(BEIJING_TZ)
    if target <= now:
        return -calc_working_hours(target, now)
    return calc_working_hours(now, target)


def hours_since(past: datetime, now: datetime | None = None) -> float:
    """
    计算从 past 到 now 的工作小时数（elapsed）。

    用于 PRD 步骤 C 计算 elapsed_working_hours。
    """
    if now is None:
        now = datetime.now(BEIJING_TZ)
    return calc_working_hours(past, now)
