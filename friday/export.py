"""Экспорт одобренных черновиков и карточек ЛПР в CSV (UTF-8-SIG для Excel / Битрикс24 / amoCRM) и Markdown."""

from __future__ import annotations

import csv
import io

from friday.signals import signal_label_ru
from friday.state import StateManager


async def export_drafts_csv(
    state: StateManager,
    *,
    status: str | None = None,
    channel: str | None = None,
) -> str:
    rows = await state.list_drafts(status=status, channel=channel, limit=1000)
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        [
            "ID черновика",
            "Статус",
            "Канал",
            "Балл ICP",
            "Компания",
            "Отрасль",
            "Город",
            "Домен",
            "ФИО ЛПР",
            "Должность",
            "Telegram",
            "Email",
            "Телефон",
            "Сигнал",
            "Тема письма",
            "Текст черновика",
            "Мини-аудит (2-3 пункта)",
        ]
    )
    for r in rows:
        fio = " ".join(p for p in [r.get("last_name", ""), r.get("first_name", ""), r.get("middle_name", "")] if p)
        tg = r.get("prospect_telegram") or r.get("company_telegram") or ""
        writer.writerow(
            [
                r.get("id", ""),
                r.get("status", ""),
                r.get("channel", ""),
                r.get("score", 0),
                r.get("company_name", ""),
                r.get("industry", ""),
                r.get("city", ""),
                r.get("domain", ""),
                fio,
                r.get("title", ""),
                tg,
                r.get("email", ""),
                r.get("prospect_phone", ""),
                signal_label_ru(r.get("signal_used", "")),
                r.get("subject", ""),
                r.get("body", ""),
                r.get("audit_summary", ""),
            ]
        )
    return buf.getvalue()


async def export_drafts_markdown(
    state: StateManager,
    *,
    status: str | None = None,
) -> str:
    rows = await state.list_drafts(status=status, limit=500)
    if not rows:
        return "# Пакет черновиков F.R.I.D.A.Y.\n\nЧерновики по заданному фильтру отсутствуют.\n"

    lines = ["# Пакет черновиков F.R.I.D.A.Y. (для ручной отправки 1-на-1)\n"]
    for idx, r in enumerate(rows, start=1):
        fio = " ".join(p for p in [r.get("last_name", ""), r.get("first_name", ""), r.get("middle_name", "")] if p) or "Руководитель"
        tg = r.get("prospect_telegram") or r.get("company_telegram") or "—"
        lines.append(f"## {idx}. {r.get('company_name') or r.get('domain')} — {fio} ({r.get('score', 0)}/100)")
        lines.append(
            f"- **Отрасль / город**: {r.get('industry') or '—'} · {r.get('city') or '—'}\n"
            f"- **Канал**: `{r.get('channel')}` | **Статус**: `{r.get('status')}` | **Сигнал**: {signal_label_ru(r.get('signal_used', ''))}\n"
            f"- **Контакты**: Telegram: {tg} | Email: {r.get('email') or '—'} | Тел.: {r.get('prospect_phone') or '—'}"
        )
        if r.get("subject"):
            lines.append(f"- **Тема**: {r['subject']}")
        if r.get("audit_summary"):
            lines.append(f"- **Шпаргалка разбора**: {r['audit_summary']}")
        lines.append("\n```text\n" + (r.get("body") or "").strip() + "\n```\n")
    return "\n".join(lines)
