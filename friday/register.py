"""Полный реестр найденных адвокатских образований и практик (`friday companies`).

Показывает по каждой практике: откуда она найдена, её оценку Fit (0–100), подтверждённые сигналы,
найденных ЛПР и понятный статус на русском языке (включая причину, если черновик ещё не создан).
"""

from __future__ import annotations

import csv
import io

from friday.config import FridayConfig
from friday.fit import company_fit
from friday.gate import display_domain, match_do_not_contact
from friday.state import StateManager

REGISTER_COLUMNS = [
    "company",
    "legal_form",
    "city",
    "website",
    "fit",
    "why_fit",
    "status",
    "status_note",
    "contacts",
    "signals",
    "source",
]


def _company_pipeline_status(
    comp: dict,
    prospects: list[dict],
    drafts: list[dict],
    dnc: list[str],
) -> tuple[str, str]:
    hit = match_do_not_contact(
        domain=comp.get("domain") or "",
        telegram=comp.get("telegram") or "",
        entries=dnc,
    )
    if hit:
        return "Исключена", f"В стоп-листе ({hit})"
    if any(p.get("status") == "opted_out" for p in prospects):
        return "Отказ от контакта", "Контакт попросил не писать"

    d_states = {d.get("status") for d in drafts}
    if "sent_manually" in d_states or any(p.get("status") == "contacted_manually" for p in prospects):
        return "Отправлено вручную", "Сообщение отправлено человеком"
    if "approved" in d_states:
        return "Одобрено к заходу", "Черновик проверен и ждёт вашей ручной отправки"
    if "pending_review" in d_states:
        return "Черновик готов", "Персональный черновик ждёт проверки во вкладке Черновики"
    if "gate_blocked" in d_states:
        return "Заблокирован Gate", "Черновик остановлен фильтром ФЗ-38 / КПЭА"

    if prospects:
        has_channel = bool(comp.get("telegram")) or any(p.get("telegram") or p.get("email") for p in prospects)
        if has_channel:
            return "Готова к черновику", "Есть ЛПР/контакт и публичный канал связи"
        return "Нет онлайн-контакта", "Найден адвокат, но на сайте только телефон/адрес"

    if comp.get("profile_checked_at"):
        return "Профилирована", "Сайт разобран, но публичный ЛПР/мессенджер не указан"
    if comp.get("domain") or comp.get("website"):
        return "Ждёт разбора сайта", "Найдена в поиске/справочнике; запустите `friday profile`"
    return "Только карточка", "У практики нет сайта (найдена в 2ГИС / картах / реестре)"


async def build_company_register(
    state: StateManager,
    config: FridayConfig,
) -> list[dict]:
    companies = await state.get_companies(limit=2000)
    prospects = await state.get_prospects(limit=4000)
    observations = await state.get_observations()
    drafts = await state.list_drafts(limit=4000)

    by_p: dict[str, list[dict]] = {}
    for p in prospects:
        by_p.setdefault(p.company_id, []).append(p.__dict__)

    by_obs: dict[str, list[dict]] = {}
    for o in observations:
        if o.get("company_id"):
            by_obs.setdefault(o["company_id"], []).append(o)

    by_d: dict[str, list[dict]] = {}
    for d in drafts:
        if d.get("company_id"):
            by_d.setdefault(d["company_id"], []).append(d)

    dnc = config.compliance.do_not_contact
    out: list[dict] = []

    for c in companies:
        c_dict = c.__dict__
        c_pros = by_p.get(c.id, [])
        c_obs = by_obs.get(c.id, [])
        c_drafts = by_d.get(c.id, [])

        fit_score, reasons = company_fit(c_dict, c_pros, c_obs, config)
        status, note = _company_pipeline_status(c_dict, c_pros, c_drafts, dnc)

        contact_strs = []
        for p in c_pros:
            fio = " ".join(x for x in (p.get("last_name"), p.get("first_name"), p.get("middle_name")) if x) or "Общий контакт"
            ch = p.get("telegram") or p.get("email") or p.get("phone") or "без контакта"
            contact_strs.append(f"{fio} ({p.get('title') or 'Адвокат'}): {ch}")

        sig_codes = sorted({o["signal_code"] for o in c_obs})

        out.append(
            {
                "id": c.id,
                "company": c.name,
                "legal_form": c.legal_form,
                "city": c.city,
                "website": c.website or (f"https://{display_domain(c.domain)}" if c.domain else ""),
                "fit": fit_score,
                "why_fit": "; ".join(reasons),
                "status": status,
                "status_note": note,
                "contacts": " | ".join(contact_strs),
                "signals": ", ".join(sig_codes),
                "tech_stack": ", ".join(c.tech_stack),
                "source": c.source,
            }
        )

    out.sort(key=lambda r: r["fit"], reverse=True)
    return out


def register_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REGISTER_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()
