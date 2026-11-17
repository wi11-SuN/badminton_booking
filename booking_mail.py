"""SMTP notifications for the booking scheduler."""

from dataclasses import dataclass, field
from datetime import datetime
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import os
import smtplib
import ssl


class MailError(RuntimeError):
    """An error suitable for the booking log. SMTP replies may contain credentials."""


@dataclass(frozen=True)
class _Settings:
    host: str
    port: int
    security: str
    user: str
    password: str = field(repr=False)
    sender: str


def _address(value, label):
    if not isinstance(value, str) or not value.strip():
        raise MailError(f"{label}需要填写一个有效邮箱地址")
    try:
        address = Address(addr_spec=value)
        if not address.username or not address.domain or any(ord(c) < 32 for c in value):
            raise ValueError
    except (HeaderParseError, ValueError, IndexError):
        raise MailError(f"{label}需要填写一个有效邮箱地址") from None
    return address.addr_spec


def _settings(recipient):
    _address(recipient, "收件人")
    host = os.environ.get("BOOKING_SMTP_HOST", "").strip()
    user = os.environ.get("BOOKING_SMTP_USER", "").strip()
    password = os.environ.get("BOOKING_SMTP_PASSWORD", "")
    if not host or not user or not password:
        raise MailError("请设置 BOOKING_SMTP_HOST、BOOKING_SMTP_USER 和 BOOKING_SMTP_PASSWORD")
    sender = _address(os.environ.get("BOOKING_SMTP_FROM", "").strip() or user, "发件人")
    security = os.environ.get("BOOKING_SMTP_SECURITY", "ssl").strip().lower()
    if security not in ("ssl", "starttls"):
        raise MailError("BOOKING_SMTP_SECURITY 支持 ssl 或 starttls")
    try:
        port = int(os.environ.get("BOOKING_SMTP_PORT", "465" if security == "ssl" else "587"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise MailError("BOOKING_SMTP_PORT 需要填写 1 至 65535 的端口号") from None
    return _Settings(host, port, security, user, password, sender)


def validate_smtp_settings(recipient):
    """Check local settings without opening a connection."""
    _settings(recipient)


def _send(recipient, subject, body):
    settings = _settings(recipient)
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=settings.sender.rsplit("@", 1)[1])
    message.set_content(body)

    try:
        context = ssl.create_default_context()
        if settings.security == "ssl":
            connection = smtplib.SMTP_SSL(settings.host, settings.port, timeout=10, context=context)
        else:
            connection = smtplib.SMTP(settings.host, settings.port, timeout=10)
        with connection as smtp:
            if settings.security == "starttls":
                smtp.starttls(context=context)
            smtp.login(settings.user, settings.password)
            smtp.send_message(message)
    except smtplib.SMTPResponseException as exc:
        raise MailError(f"邮件投递失败，SMTP 状态码 {exc.smtp_code}") from None
    except (OSError, ValueError):
        # smtplib exceptions inherit OSError. Server-supplied text is unsuitable for logs.
        raise MailError("邮件投递失败，请检查 SMTP 连接、加密方式、授权码及收件设置") from None


def send_expiration_email(recipient, detected_at):
    when = detected_at.isoformat(sep=" ", timespec="seconds") if isinstance(detected_at, datetime) else str(detected_at)
    _send(recipient, "羽毛球预约提醒：登录凭证已失效",
          f"羽毛球预约脚本于 {when} 检测到场馆登录凭证已失效。\n\n"
          "请在微信小程序重新登录，在本机运行 refresh-session，"
          "将更新后的 session.json 上传到服务器上调度服务的运行目录。")


def send_test_email(recipient):
    _send(recipient, "羽毛球预约：测试邮件",
          "你收到了羽毛球预约脚本的测试邮件，服务器上的 SMTP 发信配置已成功投递到此邮箱。")
