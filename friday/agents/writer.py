"""Агент-автор (Writer): подготовка персонализированных черновиков B2B-писем (Telegram / Email / WhatsApp).

Вариант А (`brain.mode = "agent"` — по умолчанию):
- Собирает карточку компании, ЛПР, подтверждённые сигналы из `observations` (изолированные в `<untrusted>`),
  настройки продукта из `friday.local.yaml` и `skills/product_knowledge.md`, а также правила ФЗ-38.
- Создаёт структурированные задачи `writer_draft` для ИИ-агента рабочей среды (`data/agent_tasks/`).
- Когда ИИ-агент применяет готовый текст через `friday tasks apply`, каждый черновик в обязательном
  порядке проходит детерминированный шлюз `friday.gate.validate_draft` (ФЗ-38 ст. 5 + нишевые запреты).

Режим `template` (`brain.mode = "template"` или `mode_override="template"`):
- Мгновенно собирает аккуратный предпросмотровый черновик по фактическому сигналу без ожидания агента.
"""

from __future__ import annotations

from friday.brain import enqueue_agent_task, load_prompt, load_skills
from friday.config import FridayConfig, load_config
from friday.gate import validate_draft
from friday.models.company import Company
from friday.models.draft import Draft
from friday.models.prospect import Prospect
from friday.state import StateManager
from friday.untrusted import UNTRUSTED_RULE, wrap_untrusted

HIGH_VALUE_SIGNALS = [
    "HH_HIRING_ROLE",
    "YANDEX_PAGE2_RANK",
    "YANDEX_DIRECT_ACTIVE",
    "NO_ONLINE_CAPTURE",
    "NO_YANDEX_METRIKA",
    "NO_MESSENGER_WIDGET",
    "NO_MOBILE_VIEWPORT",
    "LOW_MAP_RATING",
    "NO_WEBSITE_ON_MAP",
    "THIN_SERVICE_STRUCTURE",
    "SSL_ISSUE",
    "SLOW_SITE",
]


def _pick_primary_observation(observations: list[dict]) -> dict | None:
    by_code = {o["signal_code"]: o for o in observations}
    for code in HIGH_VALUE_SIGNALS:
        if code in by_code:
            return by_code[code]
    return observations[0] if observations else None


def _build_template_draft(
    prospect: Prospect,
    company: Company | None,
    observations: list[dict],
    channel: str,
    cfg: FridayConfig,
) -> dict:
    greeting = prospect.polite_greeting
    cname = company.name if company and company.name else (prospect.company or "вашей компании")
    dom = (company.domain or company.website) if company else ""
    city = (company.city if company and company.city else prospect.city) or "вашем регионе"
    obs = _pick_primary_observation(observations)
    codes = {o["signal_code"]: o for o in observations}

    if "HH_HIRING_ROLE" in codes and "NO_ONLINE_CAPTURE" in codes:
        sig_code = "HH_HIRING_ROLE+NO_ONLINE_CAPTURE"
        fact_sentence = (
            f"Обратил внимание, что в «{cname}» сейчас открыт подбор ({codes['HH_HIRING_ROLE']['value_text']}), "
            f"при этом на сайте {dom} пока нет быстрых форм захвата и кнопок мессенджеров."
        )
        audit_summary = (
            f"1) Сигнал роста на hh.ru ({codes['HH_HIRING_ROLE']['value_text']}). "
            f"2) На сайте {dom} отсутствует быстрый онлайн-захват обращений."
        )
    elif obs and obs["signal_code"] == "HH_HIRING_ROLE":
        sig_code = "HH_HIRING_ROLE"
        fact_sentence = (
            f"Увидел у «{cname}» открытую вакансию на hh.ru ({obs.get('value_text', '')}) "
            f"и решил написать напрямую."
        )
        audit_summary = f"1) Открытый найм на hh.ru: {obs.get('value_text', '')}."
    elif "YANDEX_DIRECT_ACTIVE" in codes and ("NO_ONLINE_CAPTURE" in codes or "NO_YANDEX_METRIKA" in codes):
        sig_code = "YANDEX_DIRECT_ACTIVE"
        fact_sentence = (
            f"Посмотрел сайт {dom}: увидел активную рекламу в Яндекс.Директе, "
            f"но заметил точку потери части обращений на посадочной странице."
        )
        audit_summary = f"1) На {dom} работает реклама Яндекс.Директа. 2) Обнаружена потеря конверсии на входе."
    elif obs and obs["signal_code"] == "YANDEX_PAGE2_RANK":
        sig_code = "YANDEX_PAGE2_RANK"
        fact_sentence = (
            f"Изучал выдачу в {city}: сайт {dom} уже стоит в шаге от первой страницы Яндекса "
            f"({obs.get('value_text', '')})."
        )
        audit_summary = f"1) {obs.get('value_text', '')}. 2) Быстрый резерв роста органического трафика."
    elif obs and obs["signal_code"] in ("LOW_MAP_RATING", "NO_WEBSITE_ON_MAP"):
        sig_code = obs["signal_code"]
        fact_sentence = (
            f"Посмотрел карточку «{cname}» в геосервисах ({city}) — заметил конкретную точку роста "
            f"по обращениям ({obs.get('value_text', '')})."
        )
        audit_summary = f"1) Карточка геосервиса: {obs.get('value_text', '')}."
    elif obs:
        sig_code = obs["signal_code"]
        fact_sentence = (
            f"Посмотрел сайт «{cname}» ({dom or city}) и отметил деталь: {obs.get('value_text', '')}."
        )
        audit_summary = f"1) Наблюдение: {obs.get('value_text', '')}."
    else:
        sig_code = "ICP_FIT"
        fact_sentence = (
            f"Познакомился с деятельностью «{cname}» в {city} — мы работаем как раз с компаниями вашей сферы."
        )
        audit_summary = f"1) Профильная компания ниши «{cfg.icp.niche}» ({city})."

    offer_raw = cfg.product.description.strip()
    max_offer_words = 26 if channel in ("telegram", "whatsapp") else 52
    offer_words = offer_raw.split()
    if len(offer_words) > max_offer_words:
        trimmed = " ".join(offer_words[:max_offer_words]).rstrip(",;:-— ")
        offer_line = trimmed if trimmed.endswith(".") else f"{trimmed}."
    else:
        offer_line = offer_raw if offer_raw.endswith(".") else f"{offer_raw}."

    cta_goal = (cfg.product.offer.entry or "прислать короткий разбор из 3 пунктов").strip().rstrip(".?")
    person_name = cfg.persona.name.strip() if cfg.persona.name and cfg.persona.name.strip() != "Не задано" else ""
    role_name = cfg.persona.role.strip() if cfg.persona.role else ""
    comp_name = cfg.persona.company.strip() if cfg.persona.company and cfg.persona.company not in ("Ваша компания", "Наш проект") else ""

    if person_name and comp_name:
        sender_sign = f"{person_name}, {comp_name}"
    elif person_name:
        sender_sign = f"{person_name}, {role_name}".strip(", ")
    elif comp_name:
        sender_sign = f"Команда {comp_name}"
    else:
        sender_sign = role_name or "Команда проекта"

    if channel in ("telegram", "whatsapp"):
        body = (
            f"{greeting}, здравствуйте. {fact_sentence}\n\n"
            f"{offer_line}\n"
            f"Подскажите, актуально ли {cta_goal} прямо сюда в чат?\n\n"
            f"С уважением, {sender_sign}"
        )
        subject = ""
    else:
        subject = f"вопрос по «{cname}» ({dom or city})"[:78]
        body = (
            f"{greeting}, добрый день.\n\n"
            f"{fact_sentence}\n\n"
            f"{offer_line}\n\n"
            f"Подскажите, уместно ли {cta_goal} в ответном письме без лишних созвонов?\n\n"
            f"С уважением,\n{sender_sign}"
        )

    return {
        "subject": subject,
        "body": body,
        "audit_summary": audit_summary,
        "signal_used": sig_code,
    }


async def generate_draft_for_prospect(
    state: StateManager,
    prospect: Prospect,
    *,
    channel: str = "telegram",
    mode_override: str | None = None,
    config: FridayConfig | None = None,
) -> dict:
    cfg = config or load_config()
    effective_mode = mode_override or cfg.brain.mode

    company = await state.get_company(prospect.company_id) if prospect.company_id else None
    observations = await state.get_observations(company_id=prospect.company_id) if prospect.company_id else []

    if effective_mode == "agent":
        # Не плодим дубликаты ожидающих задач для одного и того же ЛПР и канала
        pending = await state.list_agent_tasks(status="pending")
        for existing in pending:
            if existing["kind"] == "writer_draft":
                pl = existing.get("payload") or {}
                if pl.get("prospect_id") == prospect.id and pl.get("channel") == channel:
                    return {
                        "queued": True,
                        "already_pending": True,
                        "task_id": existing["id"],
                        "prospect_id": prospect.id,
                        "channel": channel,
                    }

        sys_prompt = "\n\n".join(
            p
            for p in [
                load_prompt("system"),
                load_prompt("writer"),
                load_skills(["product_knowledge", "competitive_intel", "ru_compliance", "outreach_frameworks_ru"]),
                UNTRUSTED_RULE,
            ]
            if p
        )
        obs_lines = "\n".join(
            f"- {o['signal_code']}: {o.get('value_text', '')} ({o.get('evidence_url', '')})"
            for o in observations
        ) or "- Нет дополнительных технических наблюдений"

        untrusted_context = wrap_untrusted(
            f"Компания: {company.name if company else prospect.company}\n"
            f"Отрасль: {company.industry if company else prospect.industry}\n"
            f"ИНН: {company.inn if company else ''}\n"
            f"Домен: {company.domain if company else ''}\n"
            f"Город: {prospect.city}\n"
            f"Описание сайта: {company.description if company else ''}\n"
            f"Технологический стек: {', '.join(company.tech_stack) if company else ''}\n"
            f"Получатель (ЛПР): {prospect.full_name} (обращение: «{prospect.polite_greeting}»)\n"
            f"Должность: {prospect.title}\n"
            f"Заметки разведки: {prospect.personalization_notes}\n"
            f"Подтверждённые сигналы:\n{obs_lines}",
            source="prospect_card",
        )
        user_prompt = (
            f"Канал сообщения: {channel}\n"
            f"Регулируемая ниша комплаенса: {cfg.compliance.regulated_niche}\n"
            f"Отправитель: {cfg.persona.name}, {cfg.persona.role} ({cfg.persona.company})\n"
            f"Продукт отправителя: {cfg.product.name} ({cfg.product.category})\n"
            f"Описание продукта: {cfg.product.description}\n"
            f"Условия / цена: {cfg.product.pricing}\n"
            f"Мягкий первый шаг (Entry Offer): {cfg.product.offer.entry}\n\n"
            f"{untrusted_context}\n\n"
            'Сформируй персональный черновик строго в формате JSON: {"subject": "...", "body": "...", "audit_summary": "...", "signal_used": "..."}.'
        )
        tid, tfile = await enqueue_agent_task(
            state,
            cfg,
            kind="writer_draft",
            payload={"prospect_id": prospect.id, "company_id": prospect.company_id, "channel": channel},
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
        )
        return {
            "queued": True,
            "task_id": tid,
            "task_file": tfile,
            "prospect_id": prospect.id,
            "channel": channel,
        }

    # Детерминированный режим предпросмотра (`template`)
    generated = _build_template_draft(prospect, company, observations, channel, cfg)
    subject = str(generated.get("subject", "")).strip()
    body = str(generated.get("body", "")).strip()
    audit_summary = str(generated.get("audit_summary", "")).strip()
    signal_used = str(generated.get("signal_used", "")).strip()

    gate_res = validate_draft(
        channel=channel,
        subject=subject,
        body=body,
        company_domain=company.domain if company else "",
        prospect_email=prospect.email,
        prospect_telegram=prospect.telegram or (company.telegram if company else ""),
        config=cfg,
    )
    status = "pending_review" if gate_res.passed else "gate_blocked"
    notes = [*gate_res.reasons, *gate_res.warnings]

    draft = Draft(
        prospect_id=prospect.id,
        company_id=prospect.company_id,
        channel=channel,
        subject=subject,
        body=body,
        audit_summary=audit_summary,
        signal_used=signal_used,
        status=status,
        gate_notes=notes,
        brain_provider="template",
    )
    did = await state.upsert_draft(draft)
    if gate_res.passed and prospect.status == "new":
        await state.set_prospect_status(prospect.id, "draft_ready")

    return {
        "ok": gate_res.passed,
        "queued": False,
        "draft_id": did,
        "prospect_id": prospect.id,
        "channel": channel,
        "status": status,
        "gate_passed": gate_res.passed,
        "gate_notes": notes,
        "provider": "template",
    }


async def run_drafts(
    state: StateManager,
    *,
    channel: str = "telegram",
    min_score: int | None = None,
    limit: int = 20,
    mode_override: str | None = None,
    config: FridayConfig | None = None,
) -> dict:
    cfg = config or load_config()
    threshold = min_score if min_score is not None else cfg.icp.min_fit_score
    prospects = await state.get_prospects(min_score=threshold, limit=limit)

    created = 0
    blocked = 0
    queued = 0

    for p in prospects:
        res = await generate_draft_for_prospect(
            state,
            p,
            channel=channel,
            mode_override=mode_override,
            config=cfg,
        )
        if res.get("queued"):
            queued += 1
        elif res.get("gate_passed"):
            created += 1
        else:
            blocked += 1

    return {
        "processed": len(prospects),
        "drafts_ready": created,
        "gate_blocked": blocked,
        "queued_agent_tasks": queued,
    }
