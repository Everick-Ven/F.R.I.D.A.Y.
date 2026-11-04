"""Экспорт базы адвокатов, практик и готовых персональных разборов (CSV + Markdown-досье).

Особенности под РФ:
- CSV сохраняется в UTF-8 с BOM (`utf-8-sig`) и разделителем `;` по запросу (или стандартным `,`),
  чтобы русскоязычный Microsoft Excel и Яндекс Таблицы открывали кириллицу без кракозябр.
- Строго фильтрует стоп-лист `compliance.do_not_contact` и отписавшихся (`opted_out`), если не указан `include_all=True`.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from friday.config import FridayConfig
from friday.gate import display_domain, match_do_not_contact
from friday.state import StateManager

CSV_COLUMNS = [
    "company_name",
    "legal_form",
    "city",
    "fio",
    "salutation",
    "title",
    "registry_number",
    "score",
    "telegram",
    "email",
    "phone",
    "website",
    "twogis_url",
    "ymaps_url",
    "personalization",
    "telegram_draft",
    "email_subject",
    "email_draft",
    "status",
]


async def collect_export_rows(
    state: StateManager,
    config: FridayConfig | None = None,
    *,
    min_score: int = 0,
    include_all: bool = False,
) -> list[dict]:
    prospects = await state.get_prospects(min_score=min_score, limit=2000)
    companies = {c.id: c for c in await state.get_companies(limit=2000)}
    drafts_all = await state.list_drafts(limit=4000)

    drafts_by_prospect: dict[str, dict[str, dict]] = {}
    for d in drafts_all:
        if d["status"] in ("rejected", "gate_blocked") and not include_all:
            continue
        drafts_by_prospect.setdefault(d["prospect_id"], {})[d["channel"]] = d

    dnc = config.compliance.do_not_contact if config else []
    style = config.persona.address_style if config else "formal_vy"

    rows: list[dict] = []
    for p in prospects:
        if not include_all and p.status == "opted_out":
            continue
        comp = companies.get(p.company_id)
        dom = comp.domain if comp else ""
        tg = p.telegram or (comp.telegram if comp else "")
        if not include_all and match_do_not_contact(domain=dom, email=p.email, telegram=tg, entries=dnc):
            continue

        p_drafts = drafts_by_prospect.get(p.id, {})
        tg_d = p_drafts.get("telegram") or p_drafts.get("tenchat") or {}
        em_d = p_drafts.get("email") or {}

        rows.append(
            {
                "company_name": (comp.name if comp else p.company),
                "legal_form": (comp.legal_form if comp else ""),
                "city": (comp.city if comp else p.city),
                "fio": p.full_name(),
                "salutation": p.polite_salutation(style),
                "title": p.title,
                "registry_number": p.registry_number,
                "score": p.score,
                "telegram": tg,
                "email": p.email,
                "phone": p.phone or (comp.phone if comp else ""),
                "website": (comp.website if comp else p.source_url) or (f"https://{display_domain(dom)}" if dom else ""),
                "twogis_url": comp.twogis_url if comp else "",
                "ymaps_url": comp.ymaps_url if comp else "",
                "personalization": p.personalization_notes,
                "telegram_draft": tg_d.get("body", ""),
                "email_subject": em_d.get("subject", ""),
                "email_draft": em_d.get("body", ""),
                "audit_summary": tg_d.get("audit_summary") or em_d.get("audit_summary") or "",
                "status": p.status,
            }
        )
    return rows


def rows_to_csv(rows: list[dict], delimiter: str = ",") -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, delimiter=delimiter, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


async def export_prospects_csv(
    state: StateManager,
    config: FridayConfig | None = None,
    *,
    out_path: str | Path | None = None,
    min_score: int = 0,
    include_all: bool = False,
    excel_ru: bool = False,
) -> tuple[int, str]:
    """Выгрузить базу контактов и черновиков в CSV (`utf-8-sig` для корректного открытия в Excel)."""
    rows = await collect_export_rows(
        state, config, min_score=min_score, include_all=include_all
    )
    delim = ";" if excel_ru else ","
    csv_text = rows_to_csv(rows, delimiter=delim)
    if out_path:
        Path(out_path).write_text(csv_text, encoding="utf-8-sig")
    return len(rows), csv_text


async def export_dossiers_markdown(
    state: StateManager,
    config: FridayConfig | None = None,
    *,
    out_path: str | Path | None = None,
    min_score: int = 40,
) -> tuple[int, str]:
    """Сформировать удобную рабочую тетрадь в Markdown для ручного аутрича человеком."""
    rows = await collect_export_rows(state, config, min_score=min_score, include_all=False)
    lines = [
        "# Рабочая тетрадь ручного аутрича — F.R.I.D.A.Y.",
        "",
        "> Только для ручной проверки и персональной отправки человеком (соблюдение ст. 18 ФЗ «О рекламе» и ст. 17 КПЭА).",
        "",
    ]
    count = 0
    for idx, r in enumerate(rows, start=1):
        count += 1
        title_line = f"## {idx}. {r['company_name'] or 'Практика'} ({r['city'] or 'РФ'}) — Score: {r['score']}/100"
        lines.append(title_line)
        lines.append("")
        if r["fio"]:
            reg_str = f", реестр № {r['registry_number']}" if r["registry_number"] else ""
            lines.append(f"- **ЛПР / Адвокат:** {r['fio']} ({r['title'] or 'Адвокат'}{reg_str})")
        if r["legal_form"]:
            lines.append(f"- **Форма образования:** {r['legal_form']}")
        contacts_parts = [
            x for x in (
                f"Telegram: `{r['telegram']}`" if r["telegram"] else "",
                f"Email: `{r['email']}`" if r["email"] else "",
                f"Тел.: `{r['phone']}`" if r["phone"] else "",
                f"Сайт: {r['website']}" if r["website"] else "",
            ) if x
        ]
        if contacts_parts:
            lines.append("- **Контакты:** " + " | ".join(contacts_parts))
        if r["personalization"]:
            lines.append(f"- **Наблюдение / угол захода:** {r['personalization']}")
        if r.get("audit_summary"):
            lines.append(f"- **Резюме разбора:** {r['audit_summary']}")
        lines.append("")
        if r["telegram_draft"]:
            lines.append("### Черновик для Telegram / TenChat")
            lines.append("```text")
            lines.append(r["telegram_draft"])
            lines.append("```")
            lines.append("")
        if r["email_draft"]:
            lines.append(f"### Черновик личного письма (Тема: `{r['email_subject']}`)")
            lines.append("```text")
            lines.append(r["email_draft"])
            lines.append("```")
            lines.append("")
        lines.append("---")
        lines.append("")

    md_text = "\n".join(lines)
    if out_path:
        Path(out_path).write_text(md_text, encoding="utf-8")
    return count, md_text
