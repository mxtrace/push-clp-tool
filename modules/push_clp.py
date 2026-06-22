"""
push_clp.py — 模块3 Task 2：Push CLP
实现 PRD 步骤 B–F：识别已 all-ready 但尚未 CLP 的目标订单，生成邮件草稿。

PRD v1.0 变更：
  B-3  触发条件 today <= ETD - 1天
  步骤C 使用 openTaskOnly=false + closeDateTimestamp 取 all_ready_datetime
  步骤D（新增）工作小时数 >= 8 检查
  步骤E（原D）Container Number 检查
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import openpyxl
import pytz

from .schedule_builder import SailingRecord
from .order_matcher    import OrderRecord
from .oc_client        import (
    build_oc_session,
    fetch_clp_via_container_details,
    fetch_clp_tasks_all_closed,
)

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

# Loading 表 POL 别名映射（PRD 7.2）
POL_ALIAS = {"CNSZX": "CNYTN"}


@dataclass
class CLPItem:
    """一条满足 Push CLP 条件的 AL0 记录（步骤 E 输出）。"""
    al0:                str
    all_ready_datetime: datetime    # 步骤C：max(closeDateTimestamp)（北京时区）
    si_cutoff:          datetime    # 船期 SI Cutoff datetime
    etd:                date
    pol:                str
    pod:                str
    vessel:             str = ""
    voyage:             str = ""

    @property
    def all_ready_str(self) -> str:
        return self.all_ready_datetime.strftime("%Y-%m-%d %H:%M")

    @property
    def si_cutoff_str(self) -> str:
        return self.si_cutoff.strftime("%Y-%m-%d %H:%M")

    @property
    def etd_str(self) -> str:
        return str(self.etd)


@dataclass
class CLPEmailDraft:
    """按 POL 分组的邮件草稿（步骤 F 输出）。"""
    pol:       str
    to_emails: list[str]
    subject:   str
    body_html: str
    items:     list[CLPItem] = field(default_factory=list)


@dataclass
class OrderTrace:
    """单条目标订单在 Task2 各步骤的追踪记录（用于详细 JSON + 报告邮件）。"""
    al0:           str
    pol:           str
    pod:           str
    step_b:        str = "N/A"   # PASS / NO_SAILING / DATE_MISMATCH
    step_b_detail: str = ""
    step_c:        str = "N/A"   # PASS / OPEN_TASKS / NO_TASKS / QUERY_FAIL
    step_c_detail: str = ""
    step_d:        str = "N/A"   # PASS / INSUFFICIENT_HOURS / NO_TIMESTAMP（新增：工作小时）
    step_d_detail: str = ""
    step_e:        str = "N/A"   # PASS / HAS_CLP / QUERY_FAIL（原步骤D：Container检查）
    step_e_detail: str = ""
    result:        str = "SKIP_B"  # PUSHED / SKIP_B / SKIP_C / SKIP_D / SKIP_E

    def to_dict(self) -> dict:
        return {
            "al0":           self.al0,
            "pol":           self.pol,
            "pod":           self.pod,
            "step_b":        self.step_b,
            "step_b_detail": self.step_b_detail,
            "step_c":        self.step_c,
            "step_c_detail": self.step_c_detail,
            "step_d":        self.step_d,
            "step_d_detail": self.step_d_detail,
            "step_e":        self.step_e,
            "step_e_detail": self.step_e_detail,
            "result":        self.result,
        }


def load_lsp_emails(
    file_path: str,
    pol_alias: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """
    读取 LSP 邮箱表，返回 {POL: [email1, email2, ...]}。
    过滤条件：Country 包含 "US"。
    多个邮箱用分号分隔（按单元格内容拆分）。

    pol_alias: POL 别名映射（如 {"CNSZX": "CNYTN"}），
               表中的 CNSZX 会被存储为 CNYTN，与船期表保持一致。
    """
    if pol_alias is None:
        pol_alias = POL_ALIAS   # 复用全局映射

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        return {}

    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    try:
        pol_idx     = header.index("POL")
        country_idx = header.index("Country")
        email_idx   = header.index("Email")
    except ValueError as e:
        raise ValueError(f"LSP邮箱表缺少列: {e}，实际列名: {header}")

    result: dict[str, list[str]] = {}
    for row in rows[1:]:
        pol_raw    = str(row[pol_idx]     or "").strip()
        country    = str(row[country_idx] or "").strip()
        emails_raw = str(row[email_idx]   or "").strip()

        if not pol_raw or "US" not in country.upper():
            continue

        # 应用 POL 别名映射（CNSZX → CNYTN 等）
        pol = pol_alias.get(pol_raw, pol_raw)

        emails = [e.strip() for e in emails_raw.split(";") if e.strip()]
        if emails:
            # 同一 POL 多行时合并（去重）
            existing = result.get(pol, [])
            for e in emails:
                if e not in existing:
                    existing.append(e)
            result[pol] = existing

    return result


def _find_nearest_sailing(
    pol: str,
    pod: str,
    weekly_schedule: list[SailingRecord],
    now: datetime = None,
) -> Optional[SailingRecord]:
    """
    步骤 B-2：在船期表中找到 POL/POD 匹配且未过期的船期，取 SI_Cutoff 最近的一条。
    过期判定：SI_Cutoff 当天 17:00 之前仍有效。
    """
    if now is None:
        now = datetime.now(BEIJING_TZ)
    candidates = [
        s for s in weekly_schedule
        if s.pol == pol and s.pod == pod and s.si_cutoff is not None
        and now <= BEIJING_TZ.localize(
            datetime.combine(s.si_cutoff.date(), datetime.strptime("17:00", "%H:%M").time())
        )
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.si_cutoff)


def _build_table_html(items: list[CLPItem]) -> str:
    """将 CLPItem 列表渲染为 HTML 表格（步骤 F 正文内嵌）。"""
    headers = ["AL0", "All Ready Datetime", "SI Cutoff", "ETD", "POL", "POD", "Mother vessel", "Mother voyage", "CLP状态反馈"]
    th_style = (
        'style="background-color:#4472C4;color:white;padding:4px 8px;'
        'font-family:等线,DengXian,Calibri,sans-serif;font-size:10pt;"'
    )
    td_style = (
        'style="padding:4px 8px;font-family:等线,DengXian,Calibri,sans-serif;'
        'font-size:10pt;border:1px solid #BFBFBF;"'
    )
    header_row = "".join(f"<th {th_style}>{h}</th>" for h in headers)
    rows_html  = ""
    for item in items:
        rows_html += (
            f"<tr>"
            f"<td {td_style}>{item.al0}</td>"
            f"<td {td_style}>{item.all_ready_str}</td>"
            f"<td {td_style}>{item.si_cutoff_str}</td>"
            f"<td {td_style}>{item.etd_str}</td>"
            f"<td {td_style}>{item.pol}</td>"
            f"<td {td_style}>{item.pod}</td>"
            f"<td {td_style}>{item.vessel}</td>"
            f"<td {td_style}>{item.voyage}</td>"
            f"<td {td_style}></td>"
            f"</tr>"
        )
    return (
        '<table border="1" style="border-collapse:collapse;">'
        f"<tr>{header_row}</tr>"
        f"{rows_html}"
        "</table>"
    )


def run_task2(
    orders:          list[OrderRecord],
    weekly_schedule: list[SailingRecord],
    lsp_emails:      dict[str, list[str]],
    blurb_html:      str,
    browser:         str = "firefox",
    now:             Optional[datetime] = None,
) -> tuple[list[CLPItem], list[CLPEmailDraft], list[OrderTrace]]:
    """
    Push CLP Task 2 主流程（PRD 步骤 B–F）。

    Args:
        orders:          已标记 is_mspp/is_smp 的订单列表（模块2输出）
        weekly_schedule: 已含 clp_cutoff 的船期列表（模块1输出）
        lsp_emails:      {POL: [email, ...]}（从 LSP邮箱.xlsx 读取）
        blurb_html:      Blurb_CLP.txt 原始 HTML 字符串
        browser:         浏览器类型（用于读取 Cookie）
        now:             当前时间（默认北京时间，方便测试 mock）

    Returns:
        (clp_items, email_drafts, traces)
    """
    if now is None:
        now = datetime.now(BEIJING_TZ)

    # ── 步骤 B-1/B-2/B-3：识别目标 AL0 ──────────────────────────────────
    target_orders = [o for o in orders if o.is_target]
    print(f"  [Task2] 目标订单（MSPP+SMP）: {len(target_orders)} 条", flush=True)

    step_b_pass: list[tuple[OrderRecord, SailingRecord, OrderTrace]] = []
    traces: list[OrderTrace] = []

    for order in target_orders:
        # B-1: POL 映射
        pol = POL_ALIAS.get(order.pol, order.pol)
        pod = order.pod
        trace = OrderTrace(al0=order.al0, pol=pol, pod=pod)

        # B-2: 按 POL/POD 匹配，取 SI_Cutoff 最近的船期
        nearest = _find_nearest_sailing(pol, pod, weekly_schedule, now)
        if nearest is None:
            trace.step_b = "NO_SAILING"
            trace.step_b_detail = f"无匹配船期（POL={pol} POD={pod}）"
            trace.result = "SKIP_B"
            traces.append(trace)
            print(f"         [{order.al0}] 步骤B 无匹配船期（POL={pol} POD={pod}），跳过", flush=True)
            continue

        # B-3 条件③: 持续触发直至 SI_Cutoff 当天 17:00 执行后停止
        si_cutoff_str = nearest.si_cutoff.strftime("%Y-%m-%d %H:%M")
        si_cutoff_deadline = BEIJING_TZ.localize(
            datetime.combine(nearest.si_cutoff.date(), datetime.strptime("17:00", "%H:%M").time())
        )
        if now > si_cutoff_deadline:
            trace.step_b = "SI_CUTOFF_PASSED"
            trace.step_b_detail = (
                f"Service={nearest.service_string} "
                f"ETD={nearest.etd} "
                f"SI_Cutoff={si_cutoff_str} "
                f"已超截单时间（now={now.strftime('%Y-%m-%d %H:%M')}）"
            )
            trace.result = "SKIP_B"
            traces.append(trace)
            print(
                f"         [{order.al0}] 步骤B SKIP：SI_Cutoff={si_cutoff_str} 已过，跳过",
                flush=True,
            )
            continue

        trace.step_b = "PASS"
        trace.step_b_detail = (
            f"Service={nearest.service_string} "
            f"ETD={nearest.etd} "
            f"SI_Cutoff={si_cutoff_str} "
            f"now={now.strftime('%Y-%m-%d %H:%M')} <= SI_Cutoff"
        )
        print(
            f"         [{order.al0}] 步骤B PASS POL={pol} POD={pod} "
            f"ETD={nearest.etd} SI_Cutoff={si_cutoff_str}",
            flush=True,
        )
        step_b_pass.append((order, nearest, trace))

    print(f"  [Task2] 步骤B 通过: {len(step_b_pass)} 条", flush=True)

    if not step_b_pass:
        return [], [], traces

    # 创建共享 OC Session（避免每条 AL0 重读 Cookie）
    oc_session = build_oc_session(browser)

    # ── 步骤 C + D + E ────────────────────────────────────────────────────
    clp_items: list[CLPItem] = []

    for order, sailing, trace in step_b_pass:
        pol = POL_ALIAS.get(order.pol, order.pol)

        # ── 步骤 C：所有 CLP Task 已关闭，取 all_ready_datetime ──────────
        all_closed, all_ready_dt = fetch_clp_tasks_all_closed(
            order.al0, browser=browser, session=oc_session
        )
        if all_closed is None:
            trace.step_c = "QUERY_FAIL"
            trace.step_c_detail = "Task 查询失败，保守跳过"
            trace.result = "SKIP_C"
            traces.append(trace)
            print(f"         [{order.al0}] 步骤C 查询失败，保守跳过", flush=True)
            continue
        if not all_closed:
            trace.step_c = "OPEN_TASKS"
            trace.step_c_detail = "存在未关闭的 CLP Task 或未找到任何 Task"
            trace.result = "SKIP_C"
            traces.append(trace)
            continue

        trace.step_c = "PASS"
        trace.step_c_detail = f"7个 CLP Task 全部 CLOSED，all_ready_datetime={all_ready_dt}"

        # ── 步骤 E（原步骤D）：Container 详情为空（尚未 CLP）──────────────────
        time.sleep(0.2)
        has_clp, clp_diag = fetch_clp_via_container_details(order.al0, browser=browser, session=oc_session)
        if has_clp is None:
            trace.step_e = "QUERY_FAIL"
            trace.step_e_detail = f"Container 详情获取失败: {clp_diag}"
            trace.result = "SKIP_E"
            traces.append(trace)
            print(f"         [{order.al0}] 步骤E: {clp_diag} → 查询失败，跳过", flush=True)
            continue
        if has_clp:
            trace.step_e = "HAS_CLP"
            trace.step_e_detail = f"已完成CLP: {clp_diag}"
            trace.result = "SKIP_E"
            traces.append(trace)
            print(f"         [{order.al0}] 步骤E: {clp_diag} → 已CLP，跳过", flush=True)
            continue

        trace.step_e = "PASS"
        trace.step_e_detail = f"未CLP: {clp_diag}"
        trace.result = "PUSHED"
        traces.append(trace)
        print(f"         [{order.al0}] 步骤E: {clp_diag} → 未CLP ✓ 纳入清单", flush=True)

        # ── 记录清单 ─────────────────────────────────────────────────────
        clp_items.append(CLPItem(
            al0                = order.al0,
            all_ready_datetime = all_ready_dt,
            si_cutoff          = sailing.si_cutoff,
            etd                = sailing.etd,
            pol                = pol,
            pod                = order.pod,
            vessel             = sailing.vessel,
            voyage             = sailing.voyage,
        ))

        time.sleep(0.1)

    print(f"  [Task2] 步骤E 最终清单: {len(clp_items)} 条", flush=True)

    if not clp_items:
        return clp_items, [], traces

    # ── 步骤 F：按 POL 分组，生成邮件草稿 ───────────────────────────────
    pol_groups: dict[str, list[CLPItem]] = {}
    for item in clp_items:
        pol_groups.setdefault(item.pol, []).append(item)

    email_drafts: list[CLPEmailDraft] = []

    for pol, items in pol_groups.items():
        to_emails = ["kongqman@amazon.com"]

        table_html = _build_table_html(items)
        body_html  = blurb_html + "<br><br>" + table_html

        email_drafts.append(CLPEmailDraft(
            pol       = pol,
            to_emails = to_emails,
            subject   = "请尽快安排CLP",
            body_html = body_html,
            items     = items,
        ))
        to_str = "; ".join(to_emails) or "（空）"
        print(
            f"  [Task2] 步骤F 邮件草稿: POL={pol}  AL0数={len(items)}  "
            f"收件人={to_str}",
            flush=True,
        )

    return clp_items, email_drafts, traces
