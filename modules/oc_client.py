"""
oc_client.py — OC 系统 API 客户端（Push CLP 版）
新增：
  - fetch_booking_detail 返回 has_clp（containers 非空判断）
  - fetch_clp_tasks_all_closed（7个 CLP Task 全关闭检查 + all_ready_datetime）
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
CONTAINER_DETAILS_API = f"{OC_BASE}/aglt/v2/api/containerV2/getContainerDetailsById"
TASK_INSTANCE_API     = f"{OC_BASE}/aglt/v2/api/everest/task-instance/{{al0}}?openTaskOnly=true"
TASK_INSTANCE_API_ALL = f"{OC_BASE}/aglt/v2/api/everest/task-instance/{{al0}}?openTaskOnly=false"

# Task 已关闭的状态值（PRD 步骤 C）
CLOSED_STATUSES = frozenset({
    "CLOSED_MISSED_SLA",
    "CLOSED_IN_SLA",
    "CLOSED_NO_SLA",
    "CLOSED_MISS_SLA_REASON",
})

# Push CLP 需要检查的 7 个 Task（openTaskOnly=false 查全量，taskStatus 需在 CLOSED_STATUSES 中）
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



def fetch_clp_via_container_details(
    al0: str,
    browser: str = "firefox",
    session: Optional[requests.Session] = None,
) -> tuple:
    """
    步骤 E：通过 getBookingById → getContainerDetailsById 判断是否已完成 CLP。

    流程：
      1. 调用 getBookingById 获取 containers 数组
      2. 若 containers 为空 → False（未 CLP）
      3. 取 containers[0].containerId（cont-... 格式内部 ID）
      4. POST getContainerDetailsById → containerDetails 非空 → True（已 CLP）

    Returns:
        (True,  diag) — 已完成 CLP
        (False, diag) — 未完成 CLP
        (None,  diag) — API 调用失败（保守跳过）
    """
    if session is None:
        session = build_oc_session(browser)

    # Step 1: 获取 Booking 的 containers 数组
    url = BOOKING_DETAIL_API.format(al0=al0)
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "SUCCESS":
            return None, f"API返回status={body.get('status')}"
        data = body.get("data") or {}
        containers = data.get("containers") or []

        if not isinstance(containers, list) or len(containers) == 0:
            return False, "containers=[]"

        # Step 2: 提取第一个 container 的内部 ID（cont-... 格式）
        first = containers[0]
        container_id = ""
        if isinstance(first, dict):
            container_id = (
                first.get("containerId") or
                first.get("id") or
                ""
            )

        if not container_id:
            # containers 非空但无法取到 ID → 保守认为已 CLP
            return True, f"containers={len(containers)}个, containerId为空(保守判定已CLP)"

        # Step 3: 调用 getContainerDetailsById
        detail_resp = session.post(
            CONTAINER_DETAILS_API,
            json={"containerId": container_id},
            timeout=15,
        )
        detail_resp.raise_for_status()
        detail_body = detail_resp.json()
        container_details = detail_body.get("containerDetails")
        if container_details is not None:
            return True, f"containers=[{container_id}] → containerDetails存在"
        else:
            return False, f"containers=[{container_id}] → containerDetails为空"

    except Exception as exc:
        return None, f"API异常: {exc}"

def fetch_clp_tasks_all_closed(
    al0: str,
    browser: str = "firefox",
    session: Optional[requests.Session] = None,
) -> tuple:
    """
    步骤 C：检查该 AL0 的 CLP 相关 Task 是否全部关闭，并获取最大关闭时间戳。

    使用 openTaskOnly=false 获取全量 Task（含已关闭）。
    - 找出所有属于 CLP_TASK_NAMES 的 Task
    - 若任意 CLP Task 的 taskStatus 不在 CLOSED_STATUSES → 返回 (False, None)
    - 若全部在 CLOSED_STATUSES → 返回 (True, max_close_dt)
    - max_close_dt = 所有 CLP Task 中最大的 closeDateTimestamp（北京时区 datetime）

    Returns:
        (True,  max_close_dt) — 全部关闭，可继续步骤 D
        (False, None)         — 存在未关闭的 Task，跳过
        (None,  None)         — API 调用失败，保守处理视为 False
    """
    if session is None:
        session = build_oc_session(browser)
    url = TASK_INSTANCE_API_ALL.format(al0=al0)
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        task_list = resp.json().get("everestTaskInstanceDataList", [])

        # 收集所有 CLP Task 的状态和关闭时间戳
        clp_tasks = []   # list of (task_id, task_status, close_ts_ms)
        for item in task_list:
            c = item.get("operatorConsoleTaskInstance")
            if not c:
                continue
            task_id = _extract_task_id(c)
            if task_id not in CLP_TASK_NAMES:
                continue

            # 提取 taskStatus 和 closeDateTimestamp（先从嵌套路径，再从顶层）
            booking_raw = c.get("relatedDimensions", {}).get("BOOKING")
            task_status = ""
            close_ts    = None

            if isinstance(booking_raw, dict):
                assignees   = booking_raw.get("taskAssignees", {})
                task_status = booking_raw.get("taskStatus") or ""
                close_ts = (
                    booking_raw.get("closeDateTimestamp")
                    or assignees.get("closeDateTimestamp")
                )

            if not task_status:
                task_status = c.get("taskStatus", "")
            if close_ts is None:
                close_ts = c.get("closeDateTimestamp")

            clp_tasks.append((task_id, str(task_status).strip(), close_ts))

        if not clp_tasks:
            print(f"         [{al0}] 步骤C：未找到任何 CLP Task（保守跳过）", flush=True)
            return False, None

        # 检查是否全部关闭
        for task_id, task_status, _ in clp_tasks:
            if task_status not in CLOSED_STATUSES:
                print(
                    f"         [{al0}] 步骤C 不通过：Task {task_id} 状态 = {task_status}",
                    flush=True,
                )
                return False, None

        # 全部关闭 → 取最大 closeDateTimestamp
        close_timestamps = [
            ts for _, _, ts in clp_tasks
            if isinstance(ts, (int, float)) and ts > 0
        ]
        max_close_dt: Optional[datetime] = None
        if close_timestamps:
            max_close_dt = datetime.fromtimestamp(max(close_timestamps) / 1000, tz=BEIJING_TZ)

        print(
            f"         [{al0}] 步骤C 通过（{len(clp_tasks)} 个 CLP Task 全关闭，"
            f"最大关闭时间={max_close_dt}）",
            flush=True,
        )
        return True, max_close_dt

    except Exception as exc:
        print(f"[WARN] fetch_clp_tasks_all_closed({al0}): {exc}")
        return None, None


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
