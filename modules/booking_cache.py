"""
booking_cache.py — Booking 详情本地缓存（SQLite）
仅对新增或超过 cache_refresh_hours 的 AL0 请求 OC API，实现增量更新。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import pytz

BEIJING_TZ = pytz.timezone("Asia/Shanghai")


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS booking_detail (
    al0                  TEXT PRIMARY KEY,
    is_mspp              INTEGER NOT NULL DEFAULT 0,
    is_smp               INTEGER NOT NULL DEFAULT 0,
    placement_option     TEXT,
    ops_login            TEXT,
    sales_login          TEXT,
    si_task_status       TEXT DEFAULT '',   -- SEND_SI_TO_LSP_LCL 最新任务状态
    customs_task_status  TEXT DEFAULT '',   -- SELLER_SEND_EXPORT_DOC_LCL 最新任务状态
    last_fetched         TEXT,              -- Booking detail 最后刷新时间
    task_last_fetched    TEXT,              -- Task 状态最后刷新时间
    si_creation_date     TEXT DEFAULT ''    -- SI Task 创建时间 (ISO)
);
"""

MIGRATE_SQL = [
    "ALTER TABLE booking_detail ADD COLUMN si_task_status TEXT DEFAULT ''",
    "ALTER TABLE booking_detail ADD COLUMN customs_task_status TEXT DEFAULT ''",
    "ALTER TABLE booking_detail ADD COLUMN task_last_fetched TEXT",
    "ALTER TABLE booking_detail ADD COLUMN si_creation_date TEXT DEFAULT ''",
]


class BookingCache:
    """
    AL0 Booking 详情的本地缓存。
    - MSPP 标记来自 MSPP Shipper List（内存中判断）
    - SMP  标记来自 OC Booking 详情页 placement_option
    - 仅在 AL0 首次出现或缓存超时后重新请求 OC
    """

    def __init__(self, db_path: str, cache_refresh_hours: int = 24):
        self.db_path = db_path
        self.refresh_delta = timedelta(hours=cache_refresh_hours)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(CREATE_TABLE_SQL)
        # 无感知迁移：为旧库补充新列
        for sql in MIGRATE_SQL:
            try:
                self._conn.execute(sql)
            except Exception:
                pass   # 列已存在，忽略
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def get(self, al0: str) -> Optional[dict]:
        """返回缓存中的 AL0 记录，不存在返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM booking_detail WHERE al0 = ?", (al0,)
        ).fetchone()
        return dict(row) if row else None

    def needs_refresh(self, al0: str) -> bool:
        """判断 AL0 是否需要重新从 OC 获取（新增 or 缓存超时）。"""
        row = self.get(al0)
        if row is None:
            return True
        last = row.get("last_fetched")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last).replace(tzinfo=BEIJING_TZ)
            return datetime.now(BEIJING_TZ) - last_dt > self.refresh_delta
        except ValueError:
            return True

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def upsert_mspp(self, al0: str, is_mspp: bool) -> None:
        """仅更新 MSPP 标记（不触及 SMP 相关字段）。"""
        self._conn.execute("""
            INSERT INTO booking_detail (al0, is_mspp, last_fetched)
            VALUES (?, ?, ?)
            ON CONFLICT(al0) DO UPDATE SET
                is_mspp      = excluded.is_mspp,
                last_fetched = COALESCE(last_fetched, excluded.last_fetched)
        """, (al0, int(is_mspp), _now_iso()))
        self._conn.commit()

    def upsert_oc_detail(self, al0: str, detail: dict) -> None:
        """
        更新 OC Booking 详情（placement_option、ops_login、sales_login）。
        同时根据 placement_option 设置 is_smp 标记。
        """
        placement_raw = str(detail.get("placement_option", "")).upper().replace(" ", "_")
        is_smp = int(placement_raw == "REGIONAL_INBOUND_CROSS_DOCK")
        self._conn.execute("""
            INSERT INTO booking_detail
                (al0, is_smp, placement_option, ops_login, sales_login, last_fetched)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(al0) DO UPDATE SET
                is_smp          = excluded.is_smp,
                placement_option= excluded.placement_option,
                ops_login       = excluded.ops_login,
                sales_login     = excluded.sales_login,
                last_fetched    = excluded.last_fetched
        """, (
            al0, is_smp,
            detail.get("placement_option", ""),
            detail.get("ops_login", ""),
            detail.get("sales_login", ""),
            _now_iso(),
        ))
        self._conn.commit()

    def upsert_task_statuses(self, al0: str, si_status: str, customs_status: str, si_creation_date: str = "") -> None:
        """更新两类 Task 的最新状态和 SI 创建时间，记录独立的 task_last_fetched 时间戳。"""
        now = _now_iso()
        self._conn.execute("""
            INSERT INTO booking_detail
                (al0, si_task_status, customs_task_status, si_creation_date, task_last_fetched, last_fetched)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(al0) DO UPDATE SET
                si_task_status      = excluded.si_task_status,
                customs_task_status = excluded.customs_task_status,
                si_creation_date    = CASE WHEN excluded.si_creation_date != '' THEN excluded.si_creation_date ELSE booking_detail.si_creation_date END,
                task_last_fetched   = excluded.task_last_fetched
        """, (al0, si_status, customs_status, si_creation_date, now, now))
        self._conn.commit()

    def needs_task_refresh(self, al0: str, refresh_hours: int = 1) -> bool:
        """判断 Task 状态是否超过 refresh_hours 小时未更新。"""
        row = self.get(al0)
        if row is None:
            return True
        last = row.get("task_last_fetched")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last).replace(tzinfo=BEIJING_TZ)
            return datetime.now(BEIJING_TZ) - last_dt > timedelta(hours=refresh_hours)
        except ValueError:
            return True

    def refresh_task_statuses(
        self,
        al0_list: list,
        oc_client_fn,
        refresh_hours: int = 1,
    ) -> dict:
        """
        增量刷新 Task 状态（SI + 报关资料），跳过近期已刷新的 AL0。

        Args:
            oc_client_fn: fn(al0) -> dict，建议传入共享 session 的 lambda
                          以避免每条重建 Session（每条都重读 Cookie 会很慢）

        Returns:
            {al0: {si_task_status, customs_task_status}}
        """
        import time as _time

        total     = len(al0_list)
        skipped   = 0
        refreshed = 0
        t0        = _time.monotonic()

        print(f"         Task缓存检查: 共 {total} 条，TTL={refresh_hours}h", flush=True)

        for i, al0 in enumerate(al0_list, 1):
            elapsed_total = _time.monotonic() - t0
            pct = int(i / total * 100)

            if self.needs_task_refresh(al0, refresh_hours):
                t1 = _time.monotonic()
                print(f"         [{i:3d}/{total}] {pct:3d}%  拉取: {al0} ...", end="", flush=True)
                info   = oc_client_fn(al0)
                if info is None:
                    info = {}
                si_st  = info.get("si_task_status",      "")
                cus_st = info.get("customs_task_status",  "")
                si_cd  = info.get("si_creation_date")
                si_cd_str = si_cd.isoformat() if si_cd else ""
                self.upsert_task_statuses(al0, si_st, cus_st, si_creation_date=si_cd_str)
                refreshed += 1
                dt = _time.monotonic() - t1
                si_tag  = si_st  if si_st  else "无SI任务"
                cus_tag = cus_st if cus_st else "无报关任务"
                # 预估剩余时间（基于已处理条数的平均速度）
                avg_per = elapsed_total / refreshed if refreshed > 0 else 0
                remaining_req = total - i
                # 已跳过的不占用时间，只算还需发请求的条数
                need_fetch_remain = sum(
                    1 for a in al0_list[i:] if self.needs_task_refresh(a, refresh_hours)
                )
                eta = int(avg_per * need_fetch_remain)
                print(
                    f"  SI={si_tag} 报关={cus_tag}  ({dt:.1f}s)  ETA≈{eta}s",
                    flush=True
                )
            else:
                skipped += 1
                # 每 20 条跳过时打印一次汇报，避免输出静默太久
                if skipped % 20 == 0 or i == total:
                    print(
                        f"         [{i:3d}/{total}] {pct:3d}%  "
                        f"缓存命中 (跳过:{skipped} 已刷新:{refreshed})",
                        flush=True,
                    )

        elapsed_total = _time.monotonic() - t0
        print(
            f"         Task状态完成: 刷新={refreshed}  跳过={skipped}  "
            f"总耗时={elapsed_total:.0f}s",
            flush=True
        )

        # 读取所有结果
        result = {}
        for al0 in al0_list:
            row = self.get(al0)
            if row:
                result[al0] = {
                    "al0":                al0,
                    "si_task_status":     row.get("si_task_status",    ""),
                    "customs_task_status":row.get("customs_task_status",""),
                    "si_creation_date":   row.get("si_creation_date",  ""),
                }
        return result

    # ------------------------------------------------------------------ #
    # 批量刷新
    # ------------------------------------------------------------------ #

    def refresh_batch(
        self,
        al0_list: list[str],
        oc_client_fn,          # Callable[[str], Optional[dict]]
        force: bool = False,
    ) -> dict[str, dict]:
        """
        对 al0_list 中需要刷新的 AL0 逐一调用 oc_client_fn 更新缓存。

        Args:
            al0_list:     待检查的 AL0 列表
            oc_client_fn: fetch_booking_detail(al0) -> dict | None
            force:        True = 忽略缓存时间，全量刷新

        Returns:
            {al0: cache_row_dict} — 所有 AL0 的最新缓存数据
        """
        total = len(al0_list)
        refreshed = 0
        for i, al0 in enumerate(al0_list):
            if force or self.needs_refresh(al0):
                detail = oc_client_fn(al0)
                if detail:
                    self.upsert_oc_detail(al0, detail)
                refreshed += 1
            # 每 50 条打印一次进度
            if (i + 1) % 50 == 0 or (i + 1) == total:
                print(f"         Booking 缓存: {i+1}/{total} 已检查，本次刷新 {refreshed} 条", flush=True)

        result = {}
        for al0 in al0_list:
            row = self.get(al0)
            if row:
                result[al0] = row
        return result

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _now_iso() -> str:
    return datetime.now(BEIJING_TZ).isoformat()
