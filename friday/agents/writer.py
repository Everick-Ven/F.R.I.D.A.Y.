"""Агент-автор (Writer) — 1-в-1 архитектура Harvey для персональных B2B-писем (Telegram / Email / WhatsApp).

Ключевой принцип (без каких-либо Python-шаблонов):
- Первое письмо пишется ИНДИВИДУАЛЬНО под каждого конкретного получателя живой языковой моделью
  (`Brain`: LLM API / OpenRouter, изолированный Claude Code CLI, Codex CLI или ИИ-агент рабочей среды)
  строго по проверенным фактам (`<untrusted>`), системному промпту `prompts/writer.md`, базам знаний `skills/*.md`,
  пользовательским правилам `writer.email_rules` и заметкам обратной связи `writing_feedback`.
- Опционально выполняется второй редакторский проход `friday.humanize` (`writer.humanizer: true`).
- Каждый черновик проходит детерминированный комплаенс-шлюз `friday.gate.validate_draft` (ФЗ-38 + КПЭА + анти-ИИ).
"""

from __future__ import annotations

import httpx

from friday.brain import (
    Brain,
    BrainError,
    BrainUnavailableError,
    enqueue_agent_task,
    extract_json_object,
    load_prompt,
    load_skills,
)
from friday.config import EmailRulesError, FridayConfig, load_config, load_email_rules
from friday.gate import validate_draft
from friday.humanize import build_humanizer_prompt, humanize_draft, humanizer_enabled
from friday.models.company import Company
from friday.models.draft import Draft
from friday.models.prospect import Prospect
from friday.signals import signal_label_ru
from friday.state import StateManager
from friday.untrusted import UNTRUSTED_RULE, wrap_untrusted

MAX_WRITE_ATTEMPTS = 3

PERSONAL_SHAPE_MESSENGER = (
    "- До 70 слов (включая приветствие и короткую подпись). Одно конкретное проверяемое наблюдение "
    "из блока ФАКТЫ выше и один предметный спокойный вопрос.\n"
    "- Без рекламной презентации продукта в лоб, без ссылок в первом сообщении, без длинных тире (—) и без «!».\n"
    "- Поле `subject` оставьте пустым (`\"\"`)."
)

PERSONAL_SHAPE_EMAIL = (
    "- До 75 слов. Одно конкретное проверяемое наблюдение из блока ФАКТЫ выше и один предметный спокойный вопрос.\n"
    "- Без рекламной презентации продукта в лоб, без ссылок в первом письме, без длинных тире (—) и без «!».\n"
    "- Поле `subject`: 2–5 слов со строчной буквы, естественно, без «КП» и без обманного «Re:»."
)


def operator_rules_block(rules: str) -> str:
    """Пользовательские правила автора писем (`writer.email_rules`), как в Harvey."""
    cleaned = (rules or "").strip()
    if not cleaned:
        return ""
    return (
        "\n\nПРАВИЛА АВТОРА (OPERATOR'S EMAIL RULES) — написаны владельцем системы. "
        "По длине, структуре, упоминанию продукта, теме и подписи эти правила заменяют базовую форму выше "
        "там, где они расходятся. Но они НИКОГДА не отменяют правило ФАКТОВ (говорить о получателе можно "
        "только то, что явно есть в блоке ФАКТЫ, ничего не выдумывая), запрет на фальшивый «Re:/Fwd:» "
        "и требования ФЗ-38 / КПЭА:\n"
        f"{cleaned}"
    )


async def _feedback_block(state: StateManager, prospect_ids: list[str]) -> str:
    """Заметки владельца с карточек черновиков (`writing_feedback`), новейшие первыми — как в Harvey."""
    try:
        notes = await state.get_writing_feedback(prospect_ids)
    except Exception:
        return ""
    if not notes:
        return ""
    lines = "\n".join(
        f"- {str(n.get('created_at') or '')[:10]} ({n.get('about') or 'черновик'}): "
        f"{' '.join(str(n.get('comment') or '').split())[:500]}"
        for n in notes
    )
    return (
        "\n\nОБРАТНАЯ СВЯЗЬ ВЛАДЕЛЬЦА (OWNER FEEDBACK) — замечания владельца системы к предыдущим "
        "черновикам, от новых к старым (свежее замечание имеет приоритет). Учтите их при написании, "
        "не нарушая правило ФАКТОВ и ограничения ФЗ-38:\n"
        f"{lines}"
    )


def _is_general_inbox(prospect: Prospect, company: Company | None) -> bool:
    if (prospect.role_kind or "").strip() == "company_inbox":
        return True
    if not (prospect.first_name or "").strip():
        return True
    if company and prospect.full_name.strip().lower() == (company.name or "").strip().lower():
        return True
    return False


async def build_writer_prompt(
    state: StateManager,
    prospect: Prospect,
    *,
    channel: str = "telegram",
    config: FridayConfig | None = None,
    retry_feedback: str = "",
) -> dict:
    """Собирает полный промпт Harvey `Writer` (системные правила + навыки + feedback + `<untrusted>` факты)."""
    cfg = config or load_config()
    company = await state.get_company(prospect.company_id) if prospect.company_id else None
    observations = await state.get_observations(company_id=prospect.company_id) if prospect.company_id else []

    rules = load_email_rules(cfg)
    address_hint = (
        "строго на «Вы» по Имени и Отчеству (если ФИО известно)"
        if cfg.persona.address_style == "formal_vy" or cfg.compliance.regulated_niche == "legal_advocates"
        else "уважительно на «Вы» по имени (или Имени Отчеству для старших руководителей)"
    )
    benefits_str = "\n".join(f"- {b}" for b in (cfg.product.key_benefits or [])) or "- Не указаны"

    target_niche_val = cfg.icp.niche or ", ".join(cfg.icp.industries)
    sys_part = load_prompt(
        "system",
        company_name=cfg.persona.company,
        product_name=cfg.product.name,
        product_description=cfg.product.description,
        target_niche=target_niche_val,
    )
    writer_part = load_prompt(
        "writer",
        persona_name=cfg.persona.name,
        persona_role=cfg.persona.role,
        persona_company=cfg.persona.company,
        persona_tone=cfg.persona.tone,
        address_style_hint=address_hint,
        product_name=cfg.product.name,
        product_description=cfg.product.description,
        product_benefits=benefits_str,
        product_pricing=cfg.product.pricing or "обсуждается индивидуально",
        entry_offer=cfg.product.offer.entry or "короткий предметный разбор по сайту и выдаче",
        target_niche=target_niche_val,
    )
    skills_part = load_skills(
        ["product_knowledge", "competitive_intel", "ru_compliance", "outreach_frameworks_ru"]
    )
    fb_part = await _feedback_block(state, [prospect.id])
    op_part = operator_rules_block(rules)

    general_inbox = _is_general_inbox(prospect, company)
    cname = (company.name if company and company.name else prospect.company) or "Организация"

    facts: list[str] = []
    if general_inbox:
        facts.append(f"- Имя получателя: неизвестно (общий контакт организации «{cname}»)")
        inbox_rule = (
            "\n- Письмо идёт на общий контакт организации (его прочитает управляющий партнёр, руководитель "
            "или помощник). Не выдумывайте имя человека; начните с «Здравствуйте.» или «Коллеги, здравствуйте.»."
        )
    else:
        facts.append(f"- Получатель (ЛПР): {prospect.full_name}")
        facts.append(f"- Вежливое обращение: {prospect.polite_greeting}")
        if prospect.title:
            facts.append(f"- Должность / статус: {prospect.title}")
        inbox_rule = (
            f"\n- Обращайтесь к конкретному человеку («{prospect.polite_greeting}, здравствуйте.») "
            "живым естественным текстом без переменных {{...}}."
        )

    facts.append(f"- Организация: {cname}")
    city_val = (company.city if company and company.city else prospect.city) or ""
    if city_val:
        facts.append(f"- Город: {city_val}")
    if company and company.address:
        facts.append(f"- Адрес офиса: {company.address}")
    if company and company.domain:
        facts.append(f"- Домен сайта: {company.domain}")
    elif company and company.ymaps_url:
        facts.append("- Сайт: отдельный сайт не указан (организация представлена карточкой в Яндекс.Картах)")
    if company and company.description:
        facts.append(f"- Описание с сайта / карточки: {company.description}")
    if company and company.tech_stack:
        facts.append(f"- Обнаруженные технологии на сайте: {', '.join(company.tech_stack[:6])}")

    non_dm_obs = [o for o in observations if o.get("signal_code") != "DECISION_MAKER_FOUND"]
    chosen_obs = (non_dm_obs or observations)[:4]
    for o in chosen_obs:
        lbl = signal_label_ru(o["signal_code"])
        detail = (o.get("value_text") or "").strip()
        facts.append(f"- Наблюдение ({lbl}): {detail}")

    if prospect.personalization_notes:
        facts.append(f"- Заметки разведки: {prospect.personalization_notes}")

    default_shape = PERSONAL_SHAPE_EMAIL if channel == "email" else PERSONAL_SHAPE_MESSENGER
    if rules:
        shape_instructions = (
            "- Следуйте разделу ПРАВИЛА АВТОРА выше по длине, структуре, упоминанию продукта и теме.\n"
            "- Пишите готовый живой текст без шаблонных скобок {{...}}."
        )
    else:
        shape_instructions = default_shape

    retry_section = ""
    if retry_feedback.strip():
        retry_section = (
            "\n\nПРЕДЫДУЩАЯ ПОПЫТКА ОТКЛОНЕНА ШЛЮЗОМ КАЧЕСТВА — ИСПРАВЬТЕ ЭТИ ОШИБКИ:\n"
            f"{retry_feedback.strip()}\n"
        )

    system_bundle = "\n\n".join(p for p in [sys_part, writer_part, skills_part] if p) + fb_part + op_part

    user_bundle = (
        f"Напишите ОДНО персональное первое сообщение для этого получателя.\n"
        f"Канал отправки: {channel}\n"
        f"Регулируемая ниша комплаенса: {cfg.compliance.regulated_niche}\n\n"
        "ФАКТЫ — всё, что достоверно известно об организации и человеке с их сайта и открытых карточек. "
        "Каждое утверждение о получателе в письме обязано опираться ТОЛЬКО на эти факты. "
        "Если факта здесь нет — вы его не знаете, ничего не додумывайте:\n"
        f"{wrap_untrusted(chr(10).join(facts), source='prospect_research')}\n\n"
        f"{UNTRUSTED_RULE}\n\n"
        "Требования к этому сообщению:\n"
        f"{shape_instructions}"
        f"{inbox_rule}"
        f"{retry_section}\n\n"
        "Помимо самого письма заполните короткую фактическую сводку `audit_summary` (1–3 коротких факта "
        "для карточки менеджера в системе, на русском языке без английских кодов) и `signal_used` "
        "(краткое русское название главного сигнала, на котором построен заход).\n\n"
        'Верните ТОЛЬКО валидный JSON строго такого вида: {"subject": "...", "body": "...", "audit_summary": "...", "signal_used": "..."}'
    )

    combined_prompt = f"{system_bundle}\n\n---\n\n{user_bundle}"
    return {
        "system_prompt": system_bundle,
        "user_prompt": user_bundle,
        "combined_prompt": combined_prompt,
        "prospect_id": prospect.id,
        "company_id": prospect.company_id,
        "channel": channel,
        "humanizer_enabled": humanizer_enabled(cfg),
    }


async def finalize_llm_draft(
    state: StateManager,
    prospect: Prospect,
    raw_output: str | dict,
    *,
    channel: str = "telegram",
    provider_used: str = "agent",
    config: FridayConfig | None = None,
) -> dict:
    """Разбирает JSON от модели, проверяет шлюзом `friday.gate` и сохраняет черновик в SQLite."""
    cfg = config or load_config()
    parsed = extract_json_object(raw_output)
    subject = str(parsed.get("subject") or "").strip() if channel == "email" else ""
    body = str(parsed.get("body") or "").strip()
    audit_summary = str(parsed.get("audit_summary") or "").strip()
    raw_sig = str(parsed.get("signal_used") or "").strip()
    signal_used = signal_label_ru(raw_sig) if raw_sig.isupper() and "_" in raw_sig else raw_sig

    if not body:
        raise BrainError("Модель вернула пустое поле `body`.")

    company = await state.get_company(prospect.company_id) if prospect.company_id else None
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
        brain_provider=provider_used,
    )
    did = await state.upsert_draft(draft)
    if gate_res.passed and prospect.status == "new":
        await state.set_prospect_status(prospect.id, "draft_ready")

    # Если в очереди висела ожидающая задача writer_draft для этого ЛПР/канала — закрываем её
    pending = await state.list_agent_tasks(status="pending")
    for t in pending:
        if t["kind"] == "writer_draft":
            pl = t.get("payload") or {}
            if pl.get("prospect_id") == prospect.id and pl.get("channel") == channel:
                await state.complete_agent_task(t["id"], str(raw_output if isinstance(raw_output, str) else parsed))

    return {
        "ok": gate_res.passed,
        "queued": False,
        "draft_id": did,
        "prospect_id": prospect.id,
        "channel": channel,
        "status": status,
        "gate_passed": gate_res.passed,
        "gate_notes": notes,
        "provider": provider_used,
    }


async def generate_draft_for_prospect(
    state: StateManager,
    prospect: Prospect,
    *,
    channel: str = "telegram",
    config: FridayConfig | None = None,
    brain: Brain | None = None,
) -> dict:
    """Генерирует персональное письмо через активный `Brain` (как в Harvey) без шаблонов."""
    cfg = config or load_config()
    try:
        load_email_rules(cfg)
    except EmailRulesError as exc:
        raise BrainError(f"Ошибка в пользовательских правилах писем (writer.email_rules): {exc}") from exc

    active_brain = brain or Brain(state=state, config=cfg)
    brain_info = await active_brain.inspect_status()

    # 1. Прямая генерация через настроенную модель (`api`, `claude` CLI или `codex` CLI)
    if brain_info["direct_inference_ready"]:
        retry_notes = ""
        last_result: dict | None = None
        for attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
            pkt = await build_writer_prompt(
                state,
                prospect,
                channel=channel,
                config=cfg,
                retry_feedback=retry_notes,
            )
            try:
                raw_text, provider_used = await active_brain.think(
                    pkt["combined_prompt"],
                    task="personal_outreach",
                )
            except httpx.HTTPError as net_exc:
                # В облачной песочнице с закрытым серверным интернетом передаём пакет в браузерный мост LLM
                if brain_info["effective_provider"] == "api" and brain_info["has_api_key"]:
                    return {
                        "ok": False,
                        "queued": False,
                        "needs_browser_llm": True,
                        "reason": str(net_exc),
                        "prompt_packet": pkt,
                        "humanizer_prompt_template": build_humanizer_prompt({"subject": "", "body": ""})
                        if humanizer_enabled(cfg)
                        else "",
                    }
                raise BrainError(f"Сетевая ошибка обращения к LLM API: {net_exc}") from net_exc

            parsed = extract_json_object(raw_text)
            if humanizer_enabled(cfg):
                parsed = await humanize_draft(active_brain, parsed, enabled=True)

            last_result = await finalize_llm_draft(
                state,
                prospect,
                parsed,
                channel=channel,
                provider_used=provider_used,
                config=cfg,
            )
            if last_result["gate_passed"]:
                return last_result
            retry_notes = "\n".join(f"- {n}" for n in last_result.get("gate_notes") or [])

        return last_result or {"ok": False, "queued": False, "status": "gate_blocked"}

    # 2. Режим ИИ-агента рабочей среды (`agent`): ставим реальную задачу с полным контекстом Harvey (без шаблонов!)
    pkt = await build_writer_prompt(state, prospect, channel=channel, config=cfg)
    pending = await state.list_agent_tasks(status="pending")
    for existing in pending:
        if existing["kind"] == "writer_draft":
            pl = existing.get("payload") or {}
            if pl.get("prospect_id") == prospect.id and pl.get("channel") == channel:
                return {
                    "ok": True,
                    "queued": True,
                    "already_pending": True,
                    "task_id": existing["id"],
                    "prospect_id": prospect.id,
                    "channel": channel,
                    "prompt_packet": pkt,
                }

    tid, tfile = await enqueue_agent_task(
        state,
        cfg,
        kind="writer_draft",
        payload={"prospect_id": prospect.id, "company_id": prospect.company_id, "channel": channel},
        system_prompt=pkt["system_prompt"],
        user_prompt=pkt["user_prompt"],
    )
    return {
        "ok": True,
        "queued": True,
        "task_id": tid,
        "task_file": tfile,
        "prospect_id": prospect.id,
        "channel": channel,
        "prompt_packet": pkt,
    }


async def run_drafts(
    state: StateManager,
    *,
    channel: str = "telegram",
    min_score: int | None = None,
    limit: int = 50,
    city: str | None = None,
    config: FridayConfig | None = None,
    brain: Brain | None = None,
) -> dict:
    cfg = config or load_config()
    threshold = min_score if min_score is not None else cfg.icp.min_fit_score

    companies = await state.get_companies(limit=max(limit * 2, 100), city=city)
    for comp in companies[:limit]:
        await state.ensure_company_prospect(comp.id)

    prospects = await state.get_prospects(min_score=threshold, city=city, limit=limit)
    if not prospects and threshold > 0:
        prospects = await state.get_prospects(min_score=0, city=city, limit=limit)

    active_brain = brain or Brain(state=state, config=cfg)
    created = 0
    blocked = 0
    queued = 0
    browser_packets: list[dict] = []

    for p in prospects:
        res = await generate_draft_for_prospect(
            state,
            p,
            channel=channel,
            config=cfg,
            brain=active_brain,
        )
        if res.get("needs_browser_llm"):
            browser_packets.append(res["prompt_packet"])
        elif res.get("queued"):
            queued += 1
        elif res.get("gate_passed"):
            created += 1
        else:
            blocked += 1

    out: dict = {
        "processed": len(prospects),
        "drafts_ready": created,
        "gate_blocked": blocked,
        "queued_agent_tasks": queued,
    }
    if browser_packets:
        out["needs_browser_llm"] = True
        out["prompt_packets"] = browser_packets
    return out
