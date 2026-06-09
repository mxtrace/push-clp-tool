"""
oc_client.py — OC 系统 API 客户端（Push CLP 版）
新增：
  - fetch_booking_detail 返回 has_clp（containers 非空判断）
  - fetch_clp_tasks_all_closed（7个 CLP Task 全关闭检查）
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Optional

import urllib3
import pytz
import requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BEIJING_TZ = pytz.timezone("Asia/Shanghai")
OC_BASE    = "https://trans-logistics-cn.amazon.com"

PLOT_API          = f"{OC_BASE}/aglt/config/supply/api/network-management/search"
BOOKING_DETAIL_API= f"{OC_BASE}/aglt/rest/bookingV2/getBookingById/{{al0}}"
TASK_INSTANCE_API = f"{OC_BASE}/aglt/v2/api/everest/task-instance/{{al0}}?openTaskOnly=true"

# Push CLP 需要检查的 7 个 Task（任意一个存在于 openTaskOnly=true 结果中 = 未关闭）
CLP_TASK_NAMES = frozenset({
    "SEND_SI_TO_LSP_LCL",
    "CARGO_EXCEPTION_LCL",
    "EXPORT_DOC_EXCEPTION_LCL",
    "REMEASUREMENT_LCL",
    "PALLETIZATION_LCL",
    "SELLER_SEND_EXPORT_DOC_LCL",
    "CUSTOMS_INSPECTION_AT_ORIGIN_CONTROL_LCL",
})


def _load_cookies(browser: str) -> dict[str, str]:
    """从本地浏览器 Cookie 数据库提取 OC 会话 Cookie。"""
    import browser_cookie3
    try:
        if browser == "firefox":
            jar = browser_cookie3.firefox(domain_name="trans-logistics-cn.amazon.com")
        elif browser in ("chrome", "edge"):
            jar = browser_cookie3.chrome(domain_name="trans-logistics-cn.amazon.com")
        else:
            raise ValueError(f"不支持的浏览器: {browser}")
        cookies = {c.name: c.value for c in jar}
        if not cookies:
            raise RuntimeError("未找到 Cookie，请确认浏览器已登录 OC 系统")
        return cookies
    except Exception as exc:
        raise RuntimeError(f"Cookie 提取失败: {exc}") from exc


def build_oc_session(browser: str) -> requests.Session:
    s = requests.Session()
    s.cookies.update(_load_cookies(browser))
    s.headers.update({
        "Accept":          "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         f"{OC_BASE}/aglt/appViews/app",
    })
    s.verify = False
    return s


def _to_beijing_ms(d: date, end_of_day: bool = False) -> int:
    hour, minute, second = (23, 59, 59) if end_of_day else (0, 0, 0)
    dt = BEIJING_TZ.localize(datetime(d.year, d.month, d.day, hour, minute, second))
    return int(dt.timestamp() * 1000)


def fetch_plot_data(etd_from: date, etd_to: date, browser: str = "firefox") -> list[dict]:
    """拉取指定 ETD 日期范围内的所有 PLOT 数据（自动分页）。"""
    session = build_oc_session(browser)
    params = {
        "actorRole":          "OCEAN_CARRIER",
        "transportationMode": "Ocean",
        "serviceType":        "Transportation",
        "tenant":             "AMAZON_FBA_INBOUND",
        "etdFrom":            _to_beijing_ms(etd_from),
        "etdTo":              _to_beijing_ms(etd_to, end_of_day=True),
        "pageNumber":         1,
        "pageSize":           200,
        "sortBy":             "serviceId",
        "sortOrder":          "ASC",
    }
    all_records: list[dict] = []
    while True:
        resp = session.get(PLOT_API, params=params, timeout=30)
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"OC 认证失败（HTTP {resp.status_code}）\n"
                "请确认已在浏览器登录 OC 系统后再运行工具。"
            )
        if resp.status_code == 500:
            raise RuntimeError(
                f"OC 服务端错误（HTTP 500）\n"
                "可能原因：浏览器 Cookie 未正确读取，请先登录 OC 系统后刷新页面，再重新运行工具。\n"
                f"URL: {resp.url}"
            )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "SUCCESS":
            raise RuntimeError(f"PLOT API 返回异常: {body.get('status')}")
        data    = body["data"]
        records = data.get("lsCompositeResources", [])
        all_records.extend(records)
        total = data.get("totalCount", len(all_records))
        print(f"         PLOT 分页 {params['pageNumber']}: 已获取 {len(all_records)}/{total} 条", flush=True)
        if len(all_records) >= total:
            break
        params["pageNumber"] += 1
        time.sleep(0.2)
    return all_records


def fetch_booking_detail(
    al0: str,
    browser: str = "firefox",
    session: Optional[requests.Session] = None,
) -> Optional[dict]:
    """
    获取单个 Booking 的详情。

    返回字段:
        placement_option: str   — 判断 SMP（值 "REGIONAL_INBOUND_CROSS_DOCK"）
        ops_login:        str
        sales_login:      str
        has_clp:          bool  — data.containers 非空 = 已完成 CLP
    """
    if session is None:
        session = build_oc_session(browser)
    url = BOOKING_DETAIL_API.format(al0=al0)
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "SUCCESS":
            return None
        data = body.get("data", {})

        op_assignees = data.get("operatorAssignees", {})

        # ops_login
        ops_login = (
            op_assignees.get("SELLER_SERVICE_OPERATOR")
            or op_assignees.get("OPERATOR")
            or data.get("assignee", "")
        )

        # sales_login（从 Shipper Profile API 获取）
        shipper_id = data.get("shipperId", "")
        sales_login = ""
        if shipper_id:
            try:
                shipper_url = f"{OC_BASE}/aglt/rest/shipper/{shipper_id}?program=FREIGHT"
                s_resp = session.get(shipper_url, timeout=10)
                if s_resp.status_code == 200:
                    s_data = s_resp.json()
                    sales_login = (s_data.get("shipper", {}).get("salesContact") or "")
            except Exception:
                pass
        if not sales_login:
            sales_login = (
                op_assignees.get("CUSTOMER_SERVICE")
                or data.get("customerService", "")
            )

        # placement_option
        placement_option = data.get("placementOption", "")

        # has_clp：containers 字段非空 = 已完成 CLP（PRD 步骤 D）
        containers = data.get("containers", [])
        has_clp = isinstance(containers, list) and len(containers) > 0

        return {
            "placement_option": str(placement_option).strip(),
            "ops_login":        str(ops_login).strip(),
            "sales_login":      str(sales_login).strip(),
            "has_clp":          has_clp,
        }
    except Exception as exc:
        print(f"[WARN] fetch_booking_detail({al0}): {exc}")
        return None


def fetch_clp_tasks_all_closed(
    al0: str,
    browser: str = "firefox",
    session: Optional[requests.Session] = None,
) -> Optional[bool]:
    """
    步骤 C：检查该 AL0 的 CLP 相关 Task 是否全部关闭。

    原理：TASK_INSTANCE_API 默认 openTaskOnly=true，只返回 open 状态的 Task。
    - 若 CLP_TASK_NAMES 中任意 Task 出现在响应里 → 该 Task 是 open → 未全关闭 → 返回 False
    - 若无一 CLP Task 出现 → 全部已关闭或从未创建 → 返回 True

    Returns:
        True  — 所有相关 Task 已关闭（或从未创建），可以继续步骤 D
        False — 存在未关闭的相关 Task，跳过该 AL0
        None  — API 调用失败，不确定（保守处理：视为 False）
    """
    if session is None:
        session = build_oc_session(browser)
    url = TASK_INSTANCE_API.format(al0=al0)
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        task_list = resp.json().get("everestTaskInstanceDataList", [])

        for item in task_list:
            c = item.get("operatorConsoleTaskInstance")
            if not c:
                continue
            task_id = _extract_task_id(c)
            if task_id in CLP_TASK_NAMES:
                print(f"         [{al0}] 步骤C 不通过：存在 open Task = {task_id}", flush=True)
                return False

        return True

    except Exception as exc:
        print(f"[WARN] fetch_clp_tasks_all_closed({al0}): {exc}")
        return None


def _extract_task_id(c: dict) -> str:
    """
    从 operatorConsoleTaskInstance 提取 taskDefinitionId。
    兼容格式A（顶层字段）和格式B（嵌套在 relatedDimensions.BOOKING）。
    """
    booking_raw = c.get("relatedDimensions", {}).get("BOOKING")
    task_id = ""

    if isinstance(booking_raw, dict):
        assignees = booking_raw.get("taskAssignees", {})
        task_id = (
            assignees.get("taskDefinitionId")
            or booking_raw.get("taskDefinitionId", "")
        )

    if not task_id:
        task_id = c.get("taskDefinitionId", "")

    return str(task_id).strip()


# ── 以下函数保留，供 booking_cache.py 的 refresh_batch 使用 ─────────────────

NOT_SENT_STATUSES = {"PENDING", "CLOSE_TO_SLA", "MISSED_SLA", "NO_SLA"}


def fetch_task_statuses(al0: str, browser: str = "firefox", session=None) -> dict:
    """
    获取 SI 和报关 Task 状态（兼容 booking_cache 使用）。
    Push CLP 主流程不直接调用此函数，由 booking_cache.refresh_task_statuses 调用。
    """
    if session is None:
        session = build_oc_session(browser)
    url = TASK_INSTANCE_API.format(al0=al0)
    result = {
        "si_creation_date":    None,
        "si_task_status":      "",
        "customs_task_status": "",
        "other_tasks_open":    False,
    }
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        task_list = resp.json().get("everestTaskInstanceDataList", [])

        si_candidates: list[tuple[int, str]] = []
        for item in task_list:
            c = item.get("operatorConsoleTaskInstance")
            if not c:
                continue
            task_id   = _extract_task_id(c)
            booking_raw = c.get("relatedDimensions", {}).get("BOOKING")
            sla_state = ""
            if isinstance(booking_raw, dict):
                assignees = booking_raw.get("taskAssignees", {})
                sla_state = (assignees.get("taskSlaState") or booking_raw.get("taskSlaState", ""))
            if not sla_state:
                sla_state = c.get("taskSlaState", "")

            ts = c.get("createdDateTimestamp")

            if task_id == "SEND_SI_TO_LSP_LCL":
                if isinstance(ts, (int, float)) and ts > 0:
                    si_candidates.append((int(ts), sla_state))
            elif task_id == "SELLER_SEND_EXPORT_DOC_LCL":
                if sla_state in NOT_SENT_STATUSES:
                    result["customs_task_status"] = sla_state
            elif task_id in ("REMEASUREMENT_LCL", "PALLETIZATION_LCL"):
                result["other_tasks_open"] = True

        if si_candidates:
            latest_ts, latest_status = max(si_candidates, key=lambda x: x[0])
            result["si_creation_date"] = datetime.fromtimestamp(latest_ts / 1000, tz=BEIJING_TZ)
            result["si_task_status"]   = latest_status

    except Exception as exc:
        print(f"[WARN] fetch_task_statuses({al0}): {exc}")
    return result


def fetch_pending_tasks(al0_list: list[str], browser: str = "firefox") -> list[dict]:
    """批量获取多个 AL0 的 Task 状态。"""
    results = []
    for al0 in al0_list:
        info = fetch_task_statuses(al0, browser=browser)
        info["al0"] = al0
        results.append(info)
        time.sleep(0.1)
    return results
