"""Копия заявок и заказов письмом на почту (SMTP Яндекса с паролем приложения)."""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import cfg
from .fmt import human_date

log = logging.getLogger("mail")

LABELS = (("name", "Имя"), ("phone", "Телефон"), ("addr", "Адрес"), ("date", "Дата"), ("time", "Время"),
          ("vol", "Что чистить"), ("sum", "Сумма, ₽"), ("src", "Откуда"), ("note", "Комментарий"))


def _send(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg.smtp_user, cfg.mail_to
    msg.set_content(body)
    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)


async def order(title: str, o: dict, extra: str = "") -> None:
    """title: «Новая заявка», «Записан», «Исправлен». Ошибка почты не мешает работе бота."""
    if not (cfg.mail_to and cfg.smtp_user and cfg.smtp_password):
        return
    lines = [f"{label}: {human_date(o[k]) + f' ({o[k]})' if k == 'date' else o[k]}"
             for k, label in LABELS if o.get(k)]
    if extra:
        lines += ["", extra]
    when = f", {human_date(o['date'])} {o.get('time') or ''}".rstrip() if o.get("date") else ""
    subject = f"{title}: {o.get('name') or 'клиент'}{when}"
    try:
        await asyncio.to_thread(_send, subject, "\n".join(lines))
    except Exception:
        log.exception("mail failed")
