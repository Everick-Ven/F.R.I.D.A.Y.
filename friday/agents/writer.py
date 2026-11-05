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
    raw_greeting = (prospect.polite_greeting or "коллеги").strip()
    greeting = raw_greeting[:1].upper() + raw_greeting[1:] if raw_greeting else "Коллеги"
    cname = company.name if company and company.name else (prospect.company or "вашей компании")
    quoted_cname = cname if ("«" in cname or '"' in cname) else f"«{cname}»"
    dom = (company.domain or "") if company else ""
    city = (company.city if company and company.city else prospect.city) or "вашем регионе"
    obs = _pick_primary_observation(observations)
    codes = {o["signal_code"]: o for o in observations}

    def _lc_first(s: str) -> str:
        st = (s or "").strip().rstrip(".")
        if not st:
            return ""
        # Не понижаем регистр аббревиатур и собственных имён (Яндекс, Tilda, VK, HTTP, SSL)
        first_word = st.split()[0]
        if first_word.isupper() or any(first_word.startswith(p) for p in ("Яндекс", "Tilda", "WordPress", "Битрикс", "VK", "Telegram")):
            return st
        return st[:1].lower() + st[1:]

    if "HH_HIRING_ROLE" in codes and "NO_ONLINE_CAPTURE" in codes:
        sig_code = "HH_HIRING_ROLE+NO_ONLINE_CAPTURE"
        fact_sentence = (
            f"Обратил внимание, что в {quoted_cname} сейчас открыт подбор ({_lc_first(codes['HH_HIRING_ROLE']['value_text'])}), "
            f"при этом на сайте {dom} пока нет быстрых форм захвата и кнопок мессенджеров."
        )
    elif obs and obs["signal_code"] == "HH_HIRING_ROLE":
        sig_code = "HH_HIRING_ROLE"
        fact_sentence = (
            f"Увидел у {quoted_cname} открытую вакансию на hh.ru ({_lc_first(obs.get('value_text', ''))}) "
            f"и решил написать напрямую."
        )
    elif "YANDEX_DIRECT_ACTIVE" in codes and ("NO_ONLINE_CAPTURE" in codes or "NO_YANDEX_METRIKA" in codes):
        sig_code = "YANDEX_DIRECT_ACTIVE"
        fact_sentence = (
            f"Посмотрел сайт {dom}: увидел активную рекламу в Яндекс.Директе, "
            f"но заметил точку потери части обращений на посадочной странице."
        )
    elif obs and obs["signal_code"] == "YANDEX_PAGE2_RANK":
        sig_code = "YANDEX_PAGE2_RANK"
        fact_sentence = (
            f"Изучал выдачу в г. {city}: сайт {dom} уже стоит в шаге от первой страницы Яндекса "
            f"({_lc_first(obs.get('value_text', ''))})."
        )
    elif obs and obs["signal_code"] in ("LOW_MAP_RATING", "NO_WEBSITE_ON_MAP"):
        sig_code = obs["signal_code"]
        fact_sentence = (
            f"Посмотрел карточку {quoted_cname} в геосервисах ({city}) — заметил конкретную точку роста: "
            f"{_lc_first(obs.get('value_text', ''))}."
        )
    elif obs:
        sig_code = obs["signal_code"]
        target_ref = f"{quoted_cname} ({dom})" if dom else f"{quoted_cname} ({city})"
        fact_sentence = (
            f"Посмотрел ресурс {target_ref} и отметил точку роста: {_lc_first(obs.get('value_text', ''))}."
        )
    else:
        sig_code = "ICP_FIT"
        target_ref = f"{quoted_cname} ({dom})" if dom else f"{quoted_cname} ({city})"
        fact_sentence = (
            f"Изучил публичный профиль {target_ref} в г. {city} — мы работаем как раз с практиками и компаниями вашего профиля."
        )

    audit_points: list[str] = []
    for idx, o in enumerate(observations[:3], start=1):
        txt = (o.get("value_text") or o["signal_code"]).strip().rstrip(".")
        audit_points.append(f"{idx}) {txt}.")
    if company and company.tech_stack:
        audit_points.append(f"Стек: {', '.join(company.tech_stack[:4])}.")
    if company and company.address:
        audit_points.append(f"Локация: {company.address}.")
    if not audit_points:
        audit_points.append(f"1) Профильная организация ниши «{cfg.icp.niche}» ({city}).")
    audit_summary = " ".join(audit_points)

    offer_raw = cfg.product.description.strip().replace("!", ".")
    max_offer_words = 30 if channel in ("telegram", "whatsapp") else 55
    offer_words = offer_raw.split()
    dangling_tail = {
        "и", "а", "но", "или", "да", "либо", "в", "во", "на", "с", "со",
        "к", "ко", "по", "из", "от", "до", "для", "при", "за", "над", "под",
        "о", "об", "у", "без", "между", "через", "учётом", "учетом",
    }
    if len(offer_words) > max_offer_words:
        cut = offer_words[:max_offer_words]
        # Предпочитаем обрезать на ближайшей запятой или точке во второй половине фразы
        comma_idx = -1
        for i in range(len(cut) - 1, max(8, len(cut) // 2), -1):
            if cut[i].endswith((",", ".", ";")):
                comma_idx = i
                break
        if comma_idx != -1:
            cut = cut[: comma_idx + 1]
        while cut and cut[-1].strip(",;:-—.").lower() in dangling_tail:
            cut.pop()
        trimmed = " ".join(cut).rstrip(",;:-— ")
        offer_line = trimmed if trimmed.endswith(".") else f"{trimmed}."
    else:
        offer_line = offer_raw if offer_raw.endswith(".") else f"{offer_raw}."

    cta_goal = (cfg.product.offer.entry or "прислать короткий разбор из 3 пунктов").strip().rstrip(".?!")
    placeholder_names = {"не задано", "ваше имя", "имя отправителя", ""}
    raw_pname = (cfg.persona.name or "").strip()
    person_name = raw_pname if raw_pname.lower() not in placeholder_names else ""
    role_name = cfg.persona.role.strip() if cfg.persona.role else ""
    placeholder_comps = {"ваша компания", "наш проект", "название компании", ""}
    raw_cname = (cfg.persona.company or "").strip()
    comp_name = raw_cname if raw_cname.lower() not in placeholder_comps else ""

    if person_name and comp_name:
        sender_sign = f"{person_name}, {comp_name}"
    elif person_name:
        sender_sign = f"{person_name}, {role_name}".strip(", ")
    elif comp_name:
        sender_sign = f"Команда {comp_name}"
    else:
        sender_sign = role_name or "Команда проекта"

    fact_sentence = fact_sentence.replace("!", ".")

    if channel in ("telegram", "whatsapp"):
        body = (
            f"{greeting}, здравствуйте. {fact_sentence}\n\n"
            f"{offer_line}\n"
            f"Подскажите, актуально ли {cta_goal} прямо сюда в чат?\n\n"
            f"С уважением, {sender_sign}"
        )
        subject = ""
    else:
        subject = f"разбор для «{cname}» ({dom or city})".replace("!", "")[:78]
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
    limit: int = 50,
    city: str | None = None,
    mode_override: str | None = None,
    config: FridayConfig | None = None,
) -> dict:
    cfg = config or load_config()
    threshold = min_score if min_score is not None else cfg.icp.min_fit_score

    # Гарантируем, что у найденных компаний в выбранном городе есть карточка получателя
    companies = await state.get_companies(limit=max(limit * 2, 100), city=city)
    for comp in companies[:limit]:
        await state.ensure_company_prospect(comp.id)

    prospects = await state.get_prospects(min_score=threshold, city=city, limit=limit)
    if not prospects and threshold > 0:
        prospects = await state.get_prospects(min_score=0, city=city, limit=limit)

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
