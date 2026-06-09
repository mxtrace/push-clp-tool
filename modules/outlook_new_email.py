"""
outlook_new_email.py — 新建 Outlook 邮件撰写窗口（Push CLP 用）
与 Push LegA 的 ReplyAll 不同，此处直接 CreateItem(0) 新建邮件。
每个 POL 生成一封独立邮件，POC 审核后手动发送。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .push_clp import CLPEmailDraft


@dataclass
class PopupResult:
    opened:    int = 0
    anomalies: list = field(default_factory=list)  # [{"pol": str, "reason": str}]


def open_clp_email_drafts(drafts: list) -> PopupResult:
    """
    主入口：对每个 CLPEmailDraft 在 Outlook 中打开新建邮件撰写窗口。

    Args:
        drafts: list[CLPEmailDraft]

    Returns:
        PopupResult（opened 数量 + 异常列表）
    """
    if not drafts:
        return PopupResult(opened=0)

    try:
        import win32com.client
    except ImportError:
        print("[WARN] pywin32 未安装，跳过 Outlook 弹窗")
        return PopupResult(
            opened=0,
            anomalies=[{"pol": "", "reason": "pywin32 未安装"}],
        )

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
    except Exception as exc:
        print(f"[WARN] 无法连接 Outlook: {exc}")
        return PopupResult(
            opened=0,
            anomalies=[{"pol": "", "reason": f"Outlook 连接失败: {exc}"}],
        )

    anomalies = []
    opened    = 0

    for draft in drafts:
        err = _open_one(outlook, draft)
        if err is None:
            opened += 1
            print(f"  [OK] POL={draft.pol}: Outlook 撰写窗口已打开", flush=True)
        else:
            print(f"  [异常] POL={draft.pol}: {err}", flush=True)
            anomalies.append({"pol": draft.pol, "reason": err})

    print(f"  [Outlook] 已打开 {opened} 封，异常 {len(anomalies)} 封", flush=True)
    return PopupResult(opened=opened, anomalies=anomalies)


def _open_one(outlook, draft) -> None | str:
    """
    为单个 CLPEmailDraft 打开 Outlook 新建邮件窗口。
    返回 None = 成功，str = 错误原因。
    """
    try:
        mail = outlook.CreateItem(0)   # 0 = olMailItem

        # 收件人（TO）：分号分隔的 LSP 邮箱
        if draft.to_emails:
            mail.To = "; ".join(draft.to_emails)

        # 主题
        mail.Subject = draft.subject

        # 正文（HTML）：Blurb + AL0 清单表格
        mail.HTMLBody = draft.body_html

        # 非阻塞弹出，等 POC 审核后手动发送
        mail.Display(False)
        return None

    except Exception as exc:
        return f"CreateItem 失败: {exc}"
