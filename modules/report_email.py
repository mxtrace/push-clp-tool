"""
report_email.py — 运行报告邮件
每次 Push CLP 运行结束后，自动发送详细报告至管理员邮箱。
包含：统计摘要 + match_trace 表格 + 附件（日志 + JSON）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .push_clp import OrderTrace


# ── HTML 样式常量 ──────────────────────────────────────────────────────────
_BASE_FONT = "font-family:等线,DengXian,Calibri,sans-serif;font-size:10pt;"
_TH_STYLE  = f'style="background-color:#4472C4;color:white;padding:5px 10px;{_BASE_FONT}"'
_TD_STYLE  = f'style="padding:5px 10px;{_BASE_FONT}border:1px solid #BFBFBF;"'
_RESULT_COLORS = {
    "PUSHED":  "#E2EFDA",   # 浅绿
    "SKIP_B":  "#F2F2F2",   # 灰
    "SKIP_C":  "#FFF2CC",   # 浅黄
    "SKIP_D":  "#FCE4D6",   # 浅橙
    "SKIP_E":  "#F4CCCC",   # 浅红
}


def _build_stats_html(stats: dict, run_time: str) -> str:
    """统计摘要 HTML 块。"""
    pushed = stats.get("final_pushed", 0)
    pushed_color = "#E2EFDA" if pushed > 0 else "#F2F2F2"
    rows = [
        ("运行时间",   run_time),
        ("Loading 表订单总数", stats.get("total_orders", "-")),
        ("MSPP 订单",          stats.get("mspp_orders", "-")),
        ("目标订单 (MSPP+SMP)", stats.get("target_orders", "-")),
        ("步骤B 通过",         stats.get("step_b_pass", "-")),
        ("步骤C 通过",         stats.get("step_c_pass", "-")),
        ("步骤D 通过（≥8h）",  stats.get("step_d_pass", "-")),
        ("步骤E 通过（未CLP）", stats.get("step_e_pass", "-")),
        ("最终推送",           f'<b style="background:{pushed_color};padding:2px 6px;">{pushed}</b>'),
    ]
    trs = ""
    for label, val in rows:
        trs += (
            f"<tr>"
            f"<td {_TD_STYLE}><b>{label}</b></td>"
            f"<td {_TD_STYLE}>{val}</td>"
            f"</tr>"
        )
    return (
        '<table border="1" style="border-collapse:collapse;min-width:320px;">'
        f"<tr><th {_TH_STYLE} colspan='2'>运行统计</th></tr>"
        f"{trs}"
        "</table>"
    )


def _build_trace_html(traces: list) -> str:
    """match_trace 明细表格 HTML。"""
    if not traces:
        return "<p style='color:#888;'>本次无目标订单处理记录。</p>"

    headers = ["AL0", "POL", "POD", "步骤B", "步骤B详情", "步骤C", "步骤C详情", "步骤D(≥8h)", "步骤D详情", "步骤E(CLP)", "步骤E详情", "结果"]
    header_row = "".join(f"<th {_TH_STYLE}>{h}</th>" for h in headers)

    rows_html = ""
    for t in traces:
        result = t.result if hasattr(t, "result") else t.get("result", "")
        bg = _RESULT_COLORS.get(result, "#FFFFFF")
        td = f'style="padding:5px 10px;{_BASE_FONT}border:1px solid #BFBFBF;background:{bg};"'

        def cell(v):
            return f"<td {td}>{v or ''}</td>"

        if hasattr(t, "al0"):
            # OrderTrace dataclass
            rows_html += (
                f"<tr>"
                + cell(t.al0) + cell(t.pol) + cell(t.pod)
                + cell(t.step_b) + cell(t.step_b_detail)
                + cell(t.step_c) + cell(t.step_c_detail)
                + cell(t.step_d) + cell(t.step_d_detail)
                + cell(getattr(t, "step_e", "")) + cell(getattr(t, "step_e_detail", ""))
                + cell(f"<b>{result}</b>")
                + "</tr>"
            )
        else:
            # dict（从 JSON 读取时）
            rows_html += (
                f"<tr>"
                + cell(t.get("al0")) + cell(t.get("pol")) + cell(t.get("pod"))
                + cell(t.get("step_b")) + cell(t.get("step_b_detail"))
                + cell(t.get("step_c")) + cell(t.get("step_c_detail"))
                + cell(t.get("step_d")) + cell(t.get("step_d_detail"))
                + cell(t.get("step_e", "")) + cell(t.get("step_e_detail", ""))
                + cell(f"<b>{result}</b>")
                + "</tr>"
            )

    return (
        "<p><b>订单匹配明细</b>（颜色：<span style='background:#E2EFDA;padding:1px 4px;'>PUSHED</span>"
        " <span style='background:#FFF2CC;padding:1px 4px;'>SKIP_C</span>"
        " <span style='background:#FCE4D6;padding:1px 4px;'>SKIP_D</span>"
        " <span style='background:#F2F2F2;padding:1px 4px;'>SKIP_B</span>）</p>"
        '<table border="1" style="border-collapse:collapse;">'
        f"<tr>{header_row}</tr>"
        f"{rows_html}"
        "</table>"
    )


def build_report_html(
    stats: dict,
    traces: list,
    run_time: str,
    anomalies: list | None = None,
) -> str:
    """组合完整报告 HTML 正文。"""
    stats_html = _build_stats_html(stats, run_time)
    trace_html = _build_trace_html(traces)

    anomaly_html = ""
    if anomalies:
        anomaly_html = (
            "<p><b style='color:red;'>Outlook 弹窗异常：</b></p><ul>"
            + "".join(f"<li>POL={a.get('pol','?')}: {a.get('reason','?')}</li>" for a in anomalies)
            + "</ul>"
        )

    return f"""<!DOCTYPE html>
<html><body style="{_BASE_FONT}">
<h3 style="color:#1F497D;">Push CLP 运行报告</h3>
{stats_html}
<br>
{trace_html}
{anomaly_html}
<br>
<p style="color:#888;font-size:9pt;">本邮件由 Push CLP Tool 自动生成，请勿回复。</p>
</body></html>"""


def send_report_email(
    stats:       dict,
    traces:      list,
    log_path:    Path,
    result_path: Path,
    cfg:         dict,
    run_time:    str,
    anomalies:   list | None = None,
) -> bool:
    """
    自动发送运行报告邮件至管理员邮箱。

    Args:
        stats:       统计字典（来自 main.py）
        traces:      OrderTrace 列表（来自 run_task2）
        log_path:    本次日志文件路径
        result_path: run_result.json 路径
        cfg:         配置字典
        run_time:    格式化运行时间字符串
        anomalies:   Outlook 弹窗异常列表

    Returns:
        True = 发送成功，False = 失败（已打印错误）
    """
    to_addr = cfg.get("report_email_to", "")
    if not to_addr:
        print("  [报告邮件] report_email_to 未配置，跳过发送", flush=True)
        return False

    try:
        import win32com.client
    except ImportError:
        print("  [报告邮件] pywin32 未安装，无法发送", flush=True)
        return False

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
    except Exception as exc:
        print(f"  [报告邮件] 无法连接 Outlook: {exc}", flush=True)
        return False

    pushed = stats.get("final_pushed", 0)
    prefix = f"[{pushed}条推送]" if pushed > 0 else "[无推送]"
    subject = f"Push CLP 运行报告 {prefix} {run_time}"

    body_html = build_report_html(stats, traces, run_time, anomalies)

    try:
        mail = outlook.CreateItem(0)   # olMailItem
        mail.To       = to_addr
        mail.Subject  = subject
        mail.HTMLBody = body_html

        # 添加附件：日志文件
        if log_path and log_path.exists():
            mail.Attachments.Add(str(log_path))

        # 添加附件：run_result.json
        if result_path and result_path.exists():
            mail.Attachments.Add(str(result_path))

        mail.Send()   # 直接发送，无弹窗
        print(f"  [报告邮件] 已发送至 {to_addr}（主题：{subject}）", flush=True)
        return True

    except Exception as exc:
        print(f"  [报告邮件] 发送失败: {exc}", flush=True)
        return False
