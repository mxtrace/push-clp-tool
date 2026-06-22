"""
main.py — Push CLP Tool 主入口
每次运行执行完整的 Task 2 流程：模块1 → 模块2 → Task2 → Outlook 弹窗 → 报告邮件。
由 Aki Scheduled Task 在工作日 09:30 / 14:00 调用。
"""
from __future__ import annotations

import json
import os
import sys
import traceback as _traceback
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import yaml

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

# ── 路径设置 ──────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from modules.oc_client        import fetch_plot_data, fetch_booking_detail, build_oc_session
from modules.schedule_builder import build_weekly_schedule, get_schedule_range
from modules.order_matcher    import (
    load_loading_table, load_mspp_shipper_list,
    apply_mspp_flag, apply_smp_and_contact_flags,
    filter_orders_by_potential_sailing,
)
from modules.booking_cache    import BookingCache
from modules.push_clp         import run_task2, load_lsp_emails
from modules.outlook_new_email import open_clp_email_drafts
from modules.report_email     import send_report_email
from modules.working_hours    import is_workday
from modules.updater          import check_and_update, cleanup_old, VERSION


class _TeeLogger:
    """同时写入 stdout 和日志文件。"""
    def __init__(self, log_path: Path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file   = open(log_path, "w", encoding="utf-8", buffering=1)
        self._stdout = sys.stdout
        self._stderr = sys.stderr

    def write(self, msg: str) -> None:
        try:
            self._stdout.write(msg)
        except UnicodeEncodeError:
            # stdout 编码（如 GBK）不支持该字符时降级输出，日志文件仍保留原文
            enc = getattr(self._stdout, 'encoding', 'ascii') or 'ascii'
            self._stdout.write(msg.encode(enc, errors='replace').decode(enc))
        self._file.write(msg)

    def flush(self) -> None:
        self._stdout.flush()
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __getattr__(self, name):
        return getattr(self._stdout, name)


def load_config(path: str = "config.yaml") -> dict:
    local_path = Path(path).parent / "config.local.yaml"
    cfg_path   = local_path if local_path.exists() else Path(path)
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _cfg_path(cfg_value: str) -> str:
    p = Path(os.path.expandvars(cfg_value))
    if not p.is_absolute():
        p = ROOT / p
    return str(p)


def load_blurb(path: str) -> str:
    """读取 Blurb_CLP.txt，返回 HTML 字符串。"""
    with open(path, encoding="utf-8-sig") as f:
        return f.read().strip()


def main() -> None:
    now = datetime.now(BEIJING_TZ)
    ts  = now.strftime("%Y%m%d_%H%M%S")

    # ── 日志 ─────────────────────────────────────────────────────────────
    log_path = ROOT / "data" / f"run_log_{ts}.txt"
    tee = _TeeLogger(log_path)
    sys.stdout = tee
    sys.stderr = tee

    print(f"[{now.strftime('%Y-%m-%d %H:%M')} CST] Push CLP Tool v{VERSION} 开始运行", flush=True)

    try:
        _run(now, log_path)
    except Exception:
        print("\n[FATAL] 发生未预期异常：", flush=True)
        print(_traceback.format_exc(), flush=True)
    finally:
        sys.stdout = tee._stdout
        sys.stderr = tee._stderr
        tee.close()
        print(f"[完成] 日志已写入: {log_path}")


def _run(now: datetime, log_path: Path) -> None:
    # ── 自动更新检查 ───────────────────────────────────────────────
    cleanup_old()
    if check_and_update():
        sys.exit(0)  # 已更新并重启，当前进程退出

    # ── 节假日检查 ────────────────────────────────────────────────────────
    if not is_workday(now.date()):
        print(f"  今天 {now.date()} 为法定节假日/周末，跳过运行", flush=True)
        return

    # ── 加载配置 ──────────────────────────────────────────────────────────
    cfg     = load_config(ROOT / "config.yaml")
    browser = cfg.get("oc_browser", "firefox")
    run_time_str = now.strftime("%Y-%m-%d %H:%M")

    # ── 模块1：构建周船期表 ───────────────────────────────────────────────
    print("  [模块1] 拉取 PLOT 数据...", flush=True)
    etd_from, etd_to = get_schedule_range(now.date())
    print(f"         船期范围: {etd_from} ~ {etd_to}", flush=True)

    # PLOT API 按 Original ETD 过滤；往前多拉 3 天，捞到近期顺延的船
    # Python 层 build_weekly_schedule 仍用原始 etd_from 过滤 Latest ETD >= 今天
    api_etd_from = etd_from - timedelta(days=3)
    plot_raw = fetch_plot_data(api_etd_from, etd_to, browser=browser)
    print(f"         PLOT 原始记录: {len(plot_raw)} 条（API 抓取范围 {api_etd_from} ~ {etd_to}）", flush=True)

    weekly_ok, weekly_anomaly = build_weekly_schedule(
        plot_raw, [], {}, etd_from, etd_to
    )
    # 过滤掉 clp_cutoff 为 None 的船期（LCL 才有 CLP cutoff）
    weekly_with_clp = [s for s in weekly_ok if s.clp_cutoff is not None]
    print(
        f"         LCL 船期: {len(weekly_ok)} 班（含 CLP Cutoff: {len(weekly_with_clp)} 班）"
        f" | 异常: {len(weekly_anomaly)} 班",
        flush=True,
    )
    if weekly_with_clp:
        s0 = weekly_with_clp[0]
        print(
            f"         示例: POL={s0.pol} POD={s0.pod} ETD={s0.etd} "
            f"CLP={s0.clp_cutoff.strftime('%Y-%m-%d %H:%M') if s0.clp_cutoff else 'N/A'}",
            flush=True,
        )

    # ── 模块2：订单类型匹配 ───────────────────────────────────────────────
    print("  [模块2] 读取 Loading 表 + MSPP 列表...", flush=True)
    loading_cols = cfg.get("loading_cols", {})
    loading_path = Path(os.path.expandvars(cfg["loading_table_path"]))
    orders = load_loading_table(
        str(loading_path),
        loading_cols,
        sheet_name = cfg.get("loading_sheet", "List"),
        header_row = int(cfg.get("loading_header_row", 1)),
    )
    print(f"         Loading 表: {len(orders)} 条", flush=True)

    # POL 别名规范化（CNSZX → CNYTN）
    pol_alias: dict = cfg.get("pol_alias", {})
    for order in orders:
        if order.pol in pol_alias:
            order.pol = pol_alias[order.pol]

    # Demo 阶段：alias 规范化后再过滤，确保 CNSZX→CNYTN 别名已生效
    from modules.order_matcher import DEMO_POL_FILTER
    orders = [o for o in orders if o.pol in DEMO_POL_FILTER]
    print(f"         POL过滤（Demo）后: {len(orders)} 条", flush=True)

    mspp_path = ROOT / cfg["mspp_list_path"] if not Path(cfg["mspp_list_path"]).is_absolute() else Path(cfg["mspp_list_path"])
    mspp_ids  = load_mspp_shipper_list(
        str(mspp_path),
        sheet_name     = cfg.get("mspp_sheet", "Summary"),
        shipper_id_col = cfg.get("mspp_col_shipper_id", "shipper_id"),
    )
    apply_mspp_flag(orders, mspp_ids)
    mspp_orders = [o for o in orders if o.is_mspp]
    print(f"         MSPP 订单: {len(mspp_orders)} 条", flush=True)

    # 获取 SMP 标记（通过 OC Booking 缓存）
    # ── 优化：先用船期表预筛，只查有匹配船期（CLP Cutoff=今天/明天）的 MSPP 订单 ──
    presort_orders = filter_orders_by_potential_sailing(
        mspp_orders, weekly_with_clp, now.date(),
        pol_alias=pol_alias,
    )
    print(
        f"         预筛后需查 SMP: {len(presort_orders)} 条"
        f"（原 MSPP {len(mspp_orders)} 条，节省 {len(mspp_orders) - len(presort_orders)} 次 API）",
        flush=True,
    )

    db_path = _cfg_path(cfg.get("db_path", "data/booking_cache.db"))
    oc_session = build_oc_session(browser)   # 共享 Session，复用认证 Cookie
    with BookingCache(db_path, cache_refresh_hours=int(cfg.get("cache_refresh_hours", 24))) as cache:
        cache_data = cache.refresh_batch(
            [o.al0 for o in presort_orders],
            lambda al0: fetch_booking_detail(al0, browser=browser, session=oc_session),
            max_workers=int(cfg.get("smp_fetch_workers", 5)),
        )
    apply_smp_and_contact_flags(orders, cache_data)

    smp_orders = [o for o in orders if o.is_target]
    print(f"         目标订单（MSPP+SMP）: {len(smp_orders)} 条", flush=True)

    # ── 读取辅助数据 ──────────────────────────────────────────────────────
    lsp_email_path = _cfg_path(cfg.get("lsp_email_path", "data/LSP邮箱.xlsx"))
    lsp_emails     = load_lsp_emails(lsp_email_path)
    print(f"         LSP 邮箱表: {len(lsp_emails)} 个 POL", flush=True)

    blurb_path = _cfg_path(cfg.get("blurb_clp_path", "data/Blurb_CLP.txt"))
    blurb_html = load_blurb(blurb_path)
    print(f"         Blurb_CLP: {len(blurb_html)} 字符", flush=True)

    # ── 统计对象（贯穿全流程收集）────────────────────────────────────────
    stats = {
        "total_orders":  len(orders),
        "mspp_orders":   len(mspp_orders),
        "target_orders": len(smp_orders),
        "step_b_pass":   0,
        "step_c_pass":   0,
        "step_d_pass":   0,
        "step_e_pass":   0,
        "final_pushed":  0,
    }

    # ── Task 2：Push CLP 主流程 ───────────────────────────────────────────
    print("  [Task2] 开始 Push CLP 流程...", flush=True)

    if not smp_orders:
        print("  本次运行无目标订单，跳过 Task2。", flush=True)
        clp_items    = []
        email_drafts = []
        traces       = []
    else:
        clp_items, email_drafts, traces = run_task2(
            orders          = orders,
            weekly_schedule = weekly_with_clp,
            lsp_emails      = lsp_emails,
            blurb_html      = blurb_html,
            browser         = browser,
            now             = now,
        )

    # ── 统计补全 ─────────────────────────────────────────────────────────
    stats["step_b_pass"]  = sum(1 for t in traces if t.step_b == "PASS")
    stats["step_c_pass"]  = sum(1 for t in traces if t.step_c == "PASS")
    stats["step_d_pass"]  = sum(1 for t in traces if t.step_d == "PASS")
    stats["step_e_pass"]  = sum(1 for t in traces if t.step_e == "PASS")
    stats["final_pushed"] = len(clp_items)

    print(f"\n  === 运行结果 ===", flush=True)
    print(f"  CLP 清单: {len(clp_items)} 条", flush=True)
    for item in clp_items:
        print(f"    {item.al0}  SI_Cutoff={item.si_cutoff_str}  AllReady={item.all_ready_str}  POL={item.pol}  POD={item.pod}", flush=True)

    # ── Outlook 弹窗（LSP 邮件草稿）──────────────────────────────────────
    popup_result = None
    if email_drafts:
        print(f"\n  [Outlook] 打开 {len(email_drafts)} 封邮件草稿...", flush=True)
        popup_result = open_clp_email_drafts(email_drafts)
        print(f"\n  邮件草稿已打开: {popup_result.opened} 封（请 POC 审核后手动发送）", flush=True)
        if popup_result.anomalies:
            print(f"  异常: {len(popup_result.anomalies)} 封", flush=True)
            for a in popup_result.anomalies:
                print(f"    POL={a['pol']}: {a['reason']}", flush=True)
    else:
        print("  无邮件草稿需要发送。", flush=True)

    anomalies = popup_result.anomalies if popup_result else []

    # ── 写入 run_result.json（详细版）────────────────────────────────────
    run_result = {
        "run_time":   run_time_str,
        "version":    VERSION,
        "stats":      stats,
        "clp_items":  [
            {
                "al0":               item.al0,
                "pol":               item.pol,
                "pod":               item.pod,
                "etd":               item.etd_str,
                "si_cutoff":         item.si_cutoff_str,
                "all_ready_datetime": item.all_ready_str,
            }
            for item in clp_items
        ],
        "match_trace":       [t.to_dict() for t in traces],
        "email_drafts_count": len(email_drafts),
        "popup_opened":       popup_result.opened if popup_result else 0,
        "popup_anomalies":    anomalies,
    }
    result_path = ROOT / "data" / "run_result.json"
    with open(result_path, "w", encoding="utf-8") as _rf:
        json.dump(run_result, _rf, ensure_ascii=False, indent=2)
    print(f"  结果已写入: {result_path}", flush=True)

    # ── 状态表：生成 push_status.xlsx ─────────────────────
    from modules.status_writer import write_status
    status_path = ROOT / "data" / "push_status.xlsx"
    write_status(
        orders      = orders,
        traces      = traces,
        clp_items   = clp_items,
        output_path = str(status_path),
        run_time    = now,
    )
    print(f"  [状态表] 已写入: {status_path}", flush=True)

    # ── 发送运行报告邮件（始终执行，含 0 推送的情况）────────────────────
    print("\n  [报告邮件] 准备发送运行报告...", flush=True)
    send_report_email(
        stats       = stats,
        traces      = traces,
        log_path    = log_path,
        result_path = result_path,
        cfg         = cfg,
        run_time    = run_time_str,
        anomalies   = anomalies,
    )

    print(f"\n  === 完成 ===", flush=True)


if __name__ == "__main__":
    main()
