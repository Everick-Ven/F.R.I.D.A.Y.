"""Агент-разведчик (Scout): скоринг соответствия нише РФ и формулировка угла захода.

В Варианте А (`brain.mode = "agent"`):
1. Мгновенно считает детерминированный базовый балл (`friday.fit`) по фактам из `observations` и ICP.
2. По запросу (`use_ai=True`) формирует структурированные задачи `scout_score` для ИИ-агента
   рабочей среды в `data/agent_tasks/` (с изоляцией внешних данных в `<untrusted>`).
"""

from __future__ import annotations

from friday.brain import enqueue_agent_task, load_prompt, load_skills
from friday.config import FridayConfig, load_config
from friday.fit import compute_fit_score
from friday.models.company import Company
from friday.models.prospect import Prospect
from friday.state import StateManager
from friday.untrusted import UNTRUSTED_RULE, wrap_untrusted


def _deterministic_angle(
    company: Company | None,
    observations: list[dict],
    reasons: list[str],
    cfg: FridayConfig,
) -> str:
    codes = {o["signal_code"]: o.get("value_text", "") for o in observations}
    cname = company.name if company else "компании"
    dom = (company.domain or company.website) if company else ""

    if "HH_HIRING_ROLE" in codes and ("NO_ONLINE_CAPTURE" in codes or "NO_MESSENGER_WIDGET" in codes):
        return (
            f"На hh.ru у {cname} открыт найм ({codes['HH_HIRING_ROLE']}), "
            f"при этом на сайте {dom} нет быстрых виджетов захвата заявок и мессенджеров."
        )
    if "HH_HIRING_ROLE" in codes:
        return f"Активный сигнал роста {cname} на hh.ru: {codes['HH_HIRING_ROLE']}."
    if "YANDEX_DIRECT_ACTIVE" in codes and ("NO_ONLINE_CAPTURE" in codes or "NO_YANDEX_METRIKA" in codes):
        return (
            f"На {dom} запущена реклама в Яндекс.Директе, но теряется часть обращений "
            f"из-за отсутствия онлайн-захвата или сквозной аналитики."
        )
    if "YANDEX_PAGE2_RANK" in codes:
        return f"Сайт {dom} находится в шаге от топ-10 Яндекса ({codes['YANDEX_PAGE2_RANK']})."
    if "LOW_MAP_RATING" in codes:
        return f"Точка роста в геосервисах у {cname}: {codes['LOW_MAP_RATING']}."
    if "NO_WEBSITE_ON_MAP" in codes:
        return f"В карточке геосервиса {cname} не указан сайт — часть клиентов уходит конкурентам."
    if "THIN_SERVICE_STRUCTURE" in codes:
        return f"На сайте {dom} узкая структура посадочных страниц под отдельные услуги/товары."
    if reasons:
        return f"Совпадение с нишей «{cfg.icp.niche}»: " + "; ".join(reasons[:2])
    return f"Профильная компания в городе {company.city if company else 'РФ'} под продукт «{cfg.product.name}»."


async def score_all_prospects(
    state: StateManager,
    *,
    use_ai: bool = False,
    limit: int = 100,
    config: FridayConfig | None = None,
) -> dict:
    cfg = config or load_config()
    prospects = await state.get_prospects(limit=limit)
    scored = 0
    queued_tasks = 0

    sys_prompt = ""
    if use_ai and cfg.brain.mode == "agent":
        sys_prompt = "\n\n".join(
            p
            for p in [
                load_prompt("system"),
                load_prompt("scout"),
                load_skills(["product_knowledge", "signal_playbook_ru", "lead_qualification_ru"]),
                UNTRUSTED_RULE,
            ]
            if p
        )

    for p in prospects:
        comp = await state.get_company(p.company_id) if p.company_id else None
        obs = await state.get_observations(company_id=p.company_id) if p.company_id else []
        det_score, reasons = compute_fit_score(comp, p, obs, cfg)
        det_angle = _deterministic_angle(comp, obs, reasons, cfg)

        await state.update_prospect_scoring(
            p.id,
            score=det_score,
            personalization_notes=det_angle,
        )
        scored += 1

        if use_ai and cfg.brain.mode == "agent":
            untrusted_blob = wrap_untrusted(
                f"Компания: {comp.name if comp else p.company}\n"
                f"Отрасль: {comp.industry if comp else p.industry}\n"
                f"Город: {p.city}\n"
                f"Домен: {comp.domain if comp else ''}\n"
                f"Описание: {comp.description if comp else ''}\n"
                f"Технологии: {', '.join(comp.tech_stack) if comp else ''}\n"
                f"ЛПР: {p.full_name} — {p.title}\n"
                f"Сигналы: {'; '.join(o.get('signal_code', '') + '=' + o.get('value_text', '') for o in obs)}",
                source="company_profile",
            )
            user_prompt = (
                f"Наша компания: {cfg.persona.company} — {cfg.product.name}\n"
                f"Описание продукта: {cfg.product.description}\n"
                f"Целевая ниша (ICP): {cfg.icp.niche}\n"
                f"Базовый детерминированный балл: {det_score}/100\n\n"
                f"{untrusted_blob}\n\n"
                'Верни строго JSON-объект: {"score": 0..100, "angle": "1-2 предложения на русском"}.'
            )
            await enqueue_agent_task(
                state,
                cfg,
                kind="scout_score",
                payload={"prospect_id": p.id, "company_id": p.company_id, "deterministic_score": det_score},
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
            )
            queued_tasks += 1

    return {"scored": scored, "queued_agent_tasks": queued_tasks}
