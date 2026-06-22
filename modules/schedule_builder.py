"""
schedule_builder.py — 模块1：周船期表构建
从 PLOT 数据过滤目标船期，解析 SI_Cutoff / CLP_Cutoff / ETD。
Push CLP 版：SailingRecord 新增 clp_cutoff 字段。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

TARGET_POL = {"CNYTN", "CNNGB", "CNXMN", "CNSHA", "CNTAO"}
TARGET_POD = {"USLAX", "USNYC", "USSAV"}


@dataclass
class SailingRecord:
    """周船期表中的一条记录。"""
    pol:                    str
    pod:                    str
    etd:                    date
    vessel:                 str
    voyage:                 str
    si_cutoff:              datetime            # PLOT SI_Cutoff 完整 datetime
    available_capacity:     float
    clp_cutoff:             Optional[datetime]  = None   # PLOT localDateTimeCLPCutoff
    service_string:         str                 = ""     # PLOT serviceString (e.g. PRX, CBX)
    anomaly:                bool                = False
    anomaly_reason:         str                 = ""


def get_schedule_range(today: Optional[date] = None) -> tuple[date, date]:
    """
    返回"今天 ~ 下周六"的起止日期（本周+下周）。
    Push CLP 使用此范围，覆盖本周和下周全部船期。
    """
    if today is None:
        today = datetime.now(BEIJING_TZ).date()
    days_to_this_sat = (5 - today.weekday()) % 7
    next_saturday = today + timedelta(days=days_to_this_sat + 7)
    return today, next_saturday


def _parse_etd(record: dict) -> Optional[date]:
    """从 PLOT 记录的 schedules[0].localDateTimeETD 解析 ETD 日期。"""
    try:
        schedules = record.get("schedules", [])
        if not schedules:
            return None
        raw = schedules[0].get("localDateTimeETD", "")
        if not raw:
            return None
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_si_cutoff(record: dict) -> Optional[datetime]:
    """从 PLOT 记录的 schedules[0].localDateTimeSICutoff 解析完整 datetime。"""
    try:
        schedules = record.get("schedules", [])
        if not schedules:
            return None
        raw = schedules[0].get("localDateTimeSICutoff", "")
        if not raw:
            return None
        dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        return BEIJING_TZ.localize(dt)
    except Exception:
        return None


def _parse_clp_cutoff(record: dict) -> Optional[datetime]:
    """从 PLOT 记录的 schedules[0].localDateTimeCLPCutoff 解析完整 datetime。"""
    try:
        schedules = record.get("schedules", [])
        if not schedules:
            return None
        raw = schedules[0].get("localDateTimeCLPCutoff", "")
        if not raw:
            return None
        dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        return BEIJING_TZ.localize(dt)
    except Exception:
        return None


def filter_plot_records(
    plot_records: list[dict],
    etd_from: date,
    etd_to: date,
    target_pol: set[str] | None = None,
    target_pod: set[str] | None = None,
) -> list[dict]:
    """
    按 PRD 条件过滤 PLOT 记录。
    """
    if target_pol is None:
        target_pol = TARGET_POL
    if target_pod is None:
        target_pod = TARGET_POD

    result = []
    for rec in plot_records:
        if rec.get("loadType")      != "LCL":       continue
        if rec.get("capacityPhase") != "SECURED":    continue
        if rec.get("serviceLevel")  != "Standard":   continue
        try:
            commitment = float(rec.get("capacityCommitmentValue", 0))
        except (ValueError, TypeError):
            commitment = 0.0
        if commitment == 0.0:
            continue
        pol = rec.get("pol", "")
        pod = rec.get("pod", "")
        if pol not in target_pol:   continue
        if pod not in target_pod:   continue
        etd = _parse_etd(rec)
        if etd is None:             continue
        if not (etd_from <= etd <= etd_to): continue
        result.append(rec)
    return result


def build_weekly_schedule(
    plot_records: list[dict],
    lcl_rows: list[dict],
    lcl_cols: dict[str, str],
    etd_from: date | None = None,
    etd_to:   date | None = None,
) -> tuple[list[SailingRecord], list[SailingRecord]]:
    """
    模1 主入口：构建周船期表。
    Push CLP 版：同时解析 clp_cutoff 字段。

    Returns:
        (ok_sailings, anomaly_sailings)
    """
    if etd_from is None or etd_to is None:
        etd_from, etd_to = get_schedule_range()

    filtered = filter_plot_records(plot_records, etd_from, etd_to)

    ok_sailings:      list[SailingRecord] = []
    anomaly_sailings: list[SailingRecord] = []

    for rec in filtered:
        pol       = rec.get("pol", "")
        pod       = rec.get("pod", "")
        etd       = _parse_etd(rec)
        si_cutoff = _parse_si_cutoff(rec)
        clp_cutoff = _parse_clp_cutoff(rec)
        vessel         = rec.get("vessel", "")
        voyage         = rec.get("voyage", "")
        # PLOT 实际字段名为 carrierServiceString（如 CBX、PRX、SAX）
        service_string = rec.get("carrierServiceString", "")
        if not service_string:
            # 兜底：从 serviceId 第2段提取（CMDU_CBX_FBA_... → CBX）
            service_id = rec.get("serviceId", "")
            parts = service_id.split("_")
            if len(parts) >= 2:
                service_string = parts[1]
        avail     = float(rec.get("capacityAvailableValue", 0))

        if etd is None or si_cutoff is None:
            anomaly_sailings.append(SailingRecord(
                pol=pol, pod=pod, etd=etd or date.today(),
                vessel=vessel, voyage=voyage,
                si_cutoff=si_cutoff or BEIJING_TZ.localize(datetime.now()),
                available_capacity=avail,
                anomaly=True, anomaly_reason="ETD 或 SI_Cutoff 解析失败",
            ))
            continue

        ok_sailings.append(SailingRecord(
            pol=pol, pod=pod, etd=etd,
            vessel=vessel, voyage=voyage,
            si_cutoff=si_cutoff,
            available_capacity=avail,
            clp_cutoff=clp_cutoff,
            service_string=service_string,
        ))

    return ok_sailings, anomaly_sailings
