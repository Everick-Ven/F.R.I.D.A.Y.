"""Агент-автор (Writer) — прямые B2B-предложения по Трауту, Райсу и классическому прямому маркетингу.

Ключевые принципы:
- Пользователь НЕ выбирает вручную модели, провайдеров, температуру или режим Humanizer.
- `Brain` сам выбирает сильнейший контур (`api` из окружения -> `claude` CLI -> `codex` CLI ->
  локальный `ollama` -> прозрачный браузерный вызов реальной LLM без ключей) + двухпроходный `Humanizer`
  и проверяет каждый черновик шлюзом `friday.gate.validate_draft` (ФЗ-38 + КПЭА + защита от ИИ-штампов).
- Никаких анонимных сообщений «от непонятно кого» и никаких глупых вопросов-опросников:
  1) чёткое узкое позиционирование отправителя в категории получателя (по Трауту и Райсу);
  2) конкретный проверенный факт по сайту/карточке и где именно теряется целевой спрос в поиске;
  3) конкретный прямой оффер с понятной пользой и простым шагом согласия в одно слово.
- Все указания и замечания владельца (`writing_feedback`) имеют высший приоритет и применяются сразу.
"""

from __future__ import annotations

import hashlib
import re
import httpx

from friday.brain import (
    Brain,
    BrainError,
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
    "- Объём 50–95 слов, разбитых на 3 коротких абзаца с воздухом:\n"
    "  1) Приветствие + чёткое узкое позиционирование отправителя (кто пишет и почему работаем именно с этой категорией бизнеса).\n"
    "  2) Конкретный факт по сайту/карточке из блока ФАКТЫ и почему из-за него часть целевых обращений в их городе уходит конкурентам в поиске.\n"
    "  3) Прямой конкретный оффер (готовый предметный разбор / выжимка без потери времени) + короткий вопрос на согласие («Прислать сюда?»).\n"
    "- Категорически запрещены глупые праздные вопросы («удобнее голосом или письменно?», «хватает ли сарафана?»), длинные тире (—) и «!».\n"
    "- Поле `subject` оставьте пустым (`\"\"`)."
)

PERSONAL_SHAPE_EMAIL = (
    "- Объём 65–125 слов, разбитых на 3 коротких деловых абзаца:\n"
    "  1) Приветствие + чёткое узкое позиционирование отправителя.\n"
    "  2) Конкретный факт по сайту/карточке из блока ФАКТЫ и следствие для поискового спроса в их городе.\n"
    "  3) Прямой конкретный оффер + простой вопрос на отправку материала.\n"
    "- Без праздных вопросов-опросников, без длинных тире (—) и без «!».\n"
    "- Поле `subject`: 3–6 слов со строчной буквы по сути (например, `разбор поискового спроса для домена`), без «КП» и без фальшивого «Re:»."
)


def operator_rules_block(rules: str) -> str:
    """Пользовательские правила автора писем (`writer.email_rules`)."""
    cleaned = (rules or "").strip()
    if not cleaned:
        return ""
    return (
        "\n\nПРАВИЛА АВТОРА — написаны владельцем системы. "
        "По длине, структуре, подаче, упоминанию продукта, теме и подписи эти правила имеют приоритет над базовой формой, "
        "при строгом соблюдении достоверности ФАКТОВ и требований ФЗ-38 / КПЭА:\n"
        f"{cleaned}"
    )


async def _load_feedback_notes(
    state: StateManager,
    prospect_id: str,
    company_id: str = "",
) -> list[dict]:
    try:
        return await state.get_writing_feedback(
            [prospect_id] if prospect_id else [],
            company_id=company_id,
            include_all_recent=True,
            limit=15,
        )
    except Exception:
        return []


def _format_feedback_block(notes: list[dict]) -> str:
    if not notes:
        return ""
    lines = []
    for n in notes:
        scope_lbl = "общее правило автора" if not (n.get("prospect_id") or "").strip() else (n.get("about") or "замечание")
        text = " ".join(str(n.get("comment") or "").split())[:600]
        if text:
            lines.append(f"- [{scope_lbl}] {text}")
    if not lines:
        return ""
    joined = "\n".join(lines)
    return (
        "\n\nОБЯЗАТЕЛЬНЫЕ УКАЗАНИЯ И ОБРАТНАЯ СВЯЗЬ ВЛАДЕЛЬЦА (ВЫСШИЙ ПРИОРИТЕТ):\n"
        "Владелец системы оставил прямые замечания к тому, как нужно писать предложения. "
        "Вы ОБЯЗАНЫ буквально выполнить каждое из этих указаний (свежие выше имеют приоритет над старыми), "
        "не выдумывая несуществующих фактов о компании:\n"
        f"{joined}"
    )


def sanitize_llm_text(text: str) -> str:
    """Мягкая очистка пунктуационных огрехов LLM без искажения смысла."""
    out = (text or "").strip()
    if not out:
        return ""
    # Убираем случайный markdown-жирный/курсив
    out = re.sub(r"\*\*([^*]+)\*\*", r"\1", out)
    out = re.sub(r"__([^_]+)__", r"\1", out)
    # Заменяем типографские тире на естественную запятую или двоеточие
    out = re.sub(r"\s*[—–]\s*", ", ", out)
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r":\s*,", ":", out)
    out = re.sub(r"\.\s*,", ".", out)
    # Восклицательные знаки меняем на спокойную точку
    out = out.replace("!", ".")
    out = re.sub(r"\.{2,}", ".", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _is_general_inbox(prospect: Prospect, company: Company | None) -> bool:
    if (prospect.role_kind or "").strip() in ("company_inbox", "company_contact"):
        return True
    if not (prospect.first_name or "").strip():
        return True
    if company and prospect.full_name.strip().lower() == (company.name or "").strip().lower():
        return True
    return False


def _auto_address_hint(cfg: FridayConfig, prospect: Prospect) -> str:
    conservative_niche = cfg.compliance.regulated_niche in ("legal_advocates", "medicine", "finance")
    senior_title = any(
        w in (prospect.title or "").lower()
        for w in ("председатель", "президент", "управляющ", "партнёр", "партнер", "генеральн", "главврач", "адвокат")
    )
    if cfg.persona.address_style == "formal_vy" or conservative_niche or senior_title:
        return "строго уважительно на «Вы» по Имени и Отчеству (если отчество известно)"
    return "уважительно на «Вы» по имени (или Имени Отчеству при указанном отчестве)"


async def _find_existing_draft_body(state: StateManager, prospect_id: str, channel: str) -> str:
    try:
        drafts = await state.list_drafts(limit=300)
        for d in drafts:
            if d.get("prospect_id") == prospect_id and d.get("channel") == channel:
                return str(d.get("body") or "").strip()
    except Exception:
        pass
    return ""


async def build_writer_prompt(
    state: StateManager,
    prospect: Prospect,
    *,
    channel: str = "telegram",
    config: FridayConfig | None = None,
    retry_feedback: str = "",
    variation_seed: str = "",
) -> dict:
    """Собирает полный контекст для генерации сильного прямого предложения с учётом всех указаний владельца."""
    cfg = config or load_config()
    company = await state.get_company(prospect.company_id) if prospect.company_id else None
    observations = await state.get_observations(company_id=prospect.company_id) if prospect.company_id else []

    rules = load_email_rules(cfg)
    address_hint = _auto_address_hint(cfg, prospect)
    benefits_str = "\n".join(f"- {b}" for b in (cfg.product.key_benefits or [])) or "- Узкая отраслевая специализация"
    target_niche_val = cfg.icp.niche or ", ".join(cfg.icp.industries)
    entry_offer_val = cfg.product.offer.entry or "прислать короткий предметный разбор из 3 конкретных узлов по сайту и поисковой выдаче"

    sys_part = load_prompt(
        "system",
        company_name=cfg.persona.company,
        product_name=cfg.product.name,
        product_description=cfg.product.description,
        target_niche=target_niche_val,
    )
    writer_part = load_prompt(
        "writer",
        persona_name=cfg.persona.name or "Партнёр проекта",
        persona_role=cfg.persona.role or "Профильная практика",
        persona_company=cfg.persona.company,
        persona_tone=cfg.persona.tone,
        address_style_hint=address_hint,
        product_name=cfg.product.name,
        product_description=cfg.product.description,
        product_benefits=benefits_str,
        product_pricing=cfg.product.pricing or "после оценки объёма задач",
        entry_offer=entry_offer_val,
        target_niche=target_niche_val,
    )
    skills_part = load_skills(["competitive_intel"])
    feedback_notes = await _load_feedback_notes(state, prospect.id, prospect.company_id or "")
    fb_part = _format_feedback_block(feedback_notes)
    op_part = operator_rules_block(rules)

    general_inbox = _is_general_inbox(prospect, company)
    cname = (company.name if company and company.name else prospect.company) or "Организация"

    facts: list[str] = []
    if general_inbox:
        facts.append(f"- Имя получателя: неизвестно (общий контакт организации «{cname}»)")
        inbox_rule = (
            "\n- Письмо идёт на общий контакт организации (его прочитает управляющий партнёр или руководитель). "
            "Не выдумывайте имя человека; начните с «Здравствуйте.» или «Коллеги, добрый день.»."
        )
    else:
        facts.append(f"- Получатель (ЛПР): {prospect.full_name}")
        facts.append(f"- Вежливое обращение: {prospect.polite_greeting}")
        if prospect.title:
            facts.append(f"- Должность / статус: {prospect.title}")
        inbox_rule = (
            f"\n- Обращайтесь к конкретному человеку («{prospect.polite_greeting}, здравствуйте.» или «{prospect.polite_greeting}, добрый день.») "
            "без переменных {{...}}."
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
        facts.append("- Сайт: отдельный сайт не привязан (организация представлена карточкой в Яндекс.Картах)")
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
            "- Следуйте разделу ПРАВИЛА АВТОРА по длине, структуре, подаче и теме.\n"
            "- Пишите готовый живой текст без шаблонных скобок {{...}}."
        )
    else:
        shape_instructions = default_shape

    prev_draft_section = ""
    if variation_seed:
        prev_body = await _find_existing_draft_body(state, prospect.id, channel)
        if prev_body:
            prev_draft_section = (
                "\n\nПРЕДЫДУЩИЙ ВАРИАНТ ЧЕРНОВИКА (НЕ ПОВТОРЯЙТЕ ЕГО ФРАЗЫ — НАПИШИТЕ НОВЫЙ, БОЛЕЕ СИЛЬНЫЙ ВАРИАНТ):\n"
                f"{wrap_untrusted(prev_body, source='previous_draft')}\n"
            )

    retry_section = ""
    if retry_feedback.strip():
        retry_section = (
            "\n\nПРЕДЫДУЩАЯ ПОПЫТКА ОТКЛОНЕНА ШЛЮЗОМ КАЧЕСТВА — ИСПРАВЬТЕ ЭТИ ОШИБКИ:\n"
            f"{retry_feedback.strip()}\n"
        )

    fb_reminder_tail = ""
    if fb_part:
        fb_reminder_tail = f"{fb_part}\n\nПроследите, чтобы итоговый текст в поле `body` в точности выполнял каждое указание владельца выше!\n\n"

    system_bundle = "\n\n".join(p for p in [sys_part, writer_part, skills_part] if p) + fb_part + op_part

    user_bundle = (
        f"Напишите ОДНО сильное персональное деловое предложение для этого получателя.\n"
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
        f"{prev_draft_section}"
        f"{retry_section}\n\n"
        f"{fb_reminder_tail}"
        "Помимо самого письма заполните короткую фактическую сводку `audit_summary` (1–3 коротких факта "
        "для карточки менеджера на русском языке без английских кодов) и `signal_used` "
        "(краткое русское название главного сигнала, на котором построен аргумент).\n\n"
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
        "feedback_count": len(feedback_notes),
    }


async def compose_autonomous_harvey_draft(
    state: StateManager,
    prospect: Prospect,
    *,
    channel: str = "telegram",
    config: FridayConfig | None = None,
    variation_seed: str = "",
) -> dict:
    """Резервный генератор прямого B2B-предложения по Трауту, Райсу и прямому маркетингу
    (используется только если сервер и браузер полностью офлайн / в автотестах без внешнего интернета),
    с полным учётом указаний из `writing_feedback`.
    """
    cfg = config or load_config()
    company = await state.get_company(prospect.company_id) if prospect.company_id else None
    observations = await state.get_observations(company_id=prospect.company_id) if prospect.company_id else []
    feedback_notes = await _load_feedback_notes(state, prospect.id, prospect.company_id or "")
    fb_joined = " ".join(str(f.get("comment") or "") for f in feedback_notes)
    fb_low = fb_joined.lower()

    want_short = any(k in fb_low for k in ("короч", "кратк", "2 предлож", "два предлож", "один абзац", "сжат"))
    skip_intro = any(k in fb_low for k in ("без представлен", "не представляйс", "убери представлен"))
    skip_addr = any(k in fb_low for k in ("без адрес", "не упоминай адрес", "не пиши адрес"))
    skip_stack = any(k in fb_low for k in ("без tilda", "без тильд", "без cms", "без стек", "не упоминай tilda"))
    no_free_word = any(k in fb_low for k in ("без слова бесплат", "убери бесплат", "не пиши бесплат"))
    want_call = any(k in fb_low for k in ("созвон", "звонок", "встреч", "10 минут", "15 минут"))

    sig_match = re.search(r"подпис(?:ывайся|ь|аться)\s*[:\-]?\s*([^\n.;]{3,70})", fb_joined, re.I)
    custom_sig = sanitize_llm_text(sig_match.group(1)) if sig_match else ""

    offer_match = re.search(r"оффер\s*[:\-]\s*([^\n]{8,160})", fb_joined, re.I)
    custom_offer_text = sanitize_llm_text(offer_match.group(1)).rstrip(".") if offer_match else ""

    focus_match = re.search(r"(?:упор|акцент)\s+на\s+([^\n.,;]{4,80})", fb_joined, re.I)
    focus_topic = sanitize_llm_text(focus_match.group(1)) if focus_match else ""

    h_bytes = hashlib.sha256(
        f"{prospect.id}:{company.id if company else ''}:{variation_seed}:{fb_joined[:120]}".encode("utf-8")
    ).digest()
    v1, v2, v3 = h_bytes[0], h_bytes[1], h_bytes[2]

    general_inbox = _is_general_inbox(prospect, company)
    if general_inbox:
        greetings = ["Здравствуйте.", "Коллеги, добрый день.", "Добрый день."]
        greet = greetings[v1 % len(greetings)]
    else:
        pg = (prospect.polite_greeting or prospect.first_name or "").strip()
        greetings = [f"{pg}, добрый день.", f"{pg}, здравствуйте."]
        greet = greetings[v1 % len(greetings)]

    cname = (company.name if company and company.name else prospect.company) or "вашей организации"
    brand_match = re.search(r"(«[^»]+»|\"[^\"]+\")", cname)
    q_cname = brand_match.group(1) if brand_match else f"«{cname}»"
    dom = (company.domain or "").strip() if company else ""
    city = ((company.city if company and company.city else prospect.city) or "").strip()
    _loc_map = {
        "москва": "в Москве",
        "санкт-петербург": "в Санкт-Петербурге",
        "ростов-на-дону": "в Ростове-на-Дону",
        "краснодар": "в Краснодаре",
        "екатеринбург": "в Екатеринбурге",
        "казань": "в Казани",
        "новосибирск": "в Новосибирске",
        "нижний новгород": "в Нижнем Новгороде",
        "самара": "в Самаре",
        "воронеж": "в Воронеже",
    }
    in_city = _loc_map.get(city.lower(), f"по городу {city}" if city else "в вашем регионе")

    stack = [s for s in (company.tech_stack if company else []) if s]
    cms_label = stack[0] if (stack and not skip_stack) else ""
    addr_short = ""
    if company and company.address and not skip_addr:
        parts = [p.strip() for p in company.address.split(",") if p.strip() and p.strip().lower() != city.lower()]
        if parts:
            addr_short = parts[0]

    is_legal = cfg.compliance.regulated_niche == "legal_advocates" or "адвокат" in (cfg.icp.niche or "").lower()
    clients_word = "доверителей" if is_legal else "целевых клиентов"
    sender_company = (cfg.persona.company or "наша команда").replace("(Pravotraffic)", "").strip()
    sender_name = (cfg.persona.name or "").strip()

    # 1. Узкое позиционирование в категории (Траут и Райс)
    if is_legal:
        pos_variants = [
            f"Пишу из бюро «{sender_company}»: мы разрабатываем сайты и выстраиваем поисковую выдачу в Яндексе исключительно для адвокатских образований с соблюдением ст. 17 КПЭА.",
            f"Мы в «{sender_company}» специализируемся только на адвокатских практиках: проектируем структуру сайтов и органический поиск в Яндексе строго в рамках ФЗ-63 и ст. 17 КПЭА.",
            f"Это профильное бюро «{sender_company}», занимаемся сайтами и поисковым спросом в Яндексе узко для коллегий и адвокатских бюро без нарушений КПЭА.",
        ]
    else:
        niche_clean = (cfg.icp.niche or "профильных B2B-компаний").lower()
        prod_clean = (cfg.product.name or "поисковое продвижение и разработка").lower()
        pos_variants = [
            f"Пишу из «{sender_company}»: мы узко занимаемся направлением «{prod_clean}» специально для сегмента {niche_clean}.",
            f"Мы в «{sender_company}» сфокусированы на одной задаче для {niche_clean}: {prod_clean}.",
        ]
    pos_line = "" if skip_intro else pos_variants[v1 % len(pos_variants)]

    # 2. Конкретный факт и рыночная точка потери спроса
    by_code = {o["signal_code"]: o for o in observations if o.get("signal_code") != "DECISION_MAKER_FOUND"}
    focus_clause = f" (особенно по направлению: {focus_topic})" if focus_topic else ""

    sig_used = "Позиция сайта в поисковом спросе"
    if "HH_HIRING_ROLE" in by_code:
        raw_vac = (by_code["HH_HIRING_ROLE"].get("value_text") or "профильного специалиста").replace("Вакансия:", "").strip(" «».")
        sig_used = signal_label_ru("HH_HIRING_ROLE")
        diag_variants = [
            f"Увидел открытую вакансию {q_cname} ({raw_vac}) на hh.ru{focus_clause}: при росте практики основной резерв обращений {in_city} сейчас лежит в узких коммерческих запросах Яндекса{f', которые {dom} пока не забирает отдельными страницами' if dom else ''}.",
            f"Обратил внимание на расширение команды {q_cname} ({raw_vac}): при этом в поиске Яндекса {in_city} предметный спрос {clients_word}{focus_clause} сейчас перехватывают практики с детальной разбивкой разделов.",
        ]
    elif "NO_WEBSITE_ON_MAP" in by_code or (not dom and company and (company.ymaps_url or addr_short)):
        sig_used = signal_label_ru("NO_WEBSITE_ON_MAP")
        loc_phrase = f" ({addr_short})" if addr_short else f" {in_city}"
        diag_variants = [
            f"В карточке {q_cname} в Яндекс.Картах{loc_phrase} не привязан сайт с разбивкой по практикам{focus_clause}, поэтому весь прямой спрос {clients_word} в поиске Яндекса {in_city} сейчас забирают агрегаторы и соседние бюро.",
            f"Посмотрел профиль {q_cname}{loc_phrase}: в геосервисах нет ссылки на сайт с отдельными страницами специализаций{focus_clause}, из-за чего горячие поисковые запросы {clients_word} {in_city} проходят мимо практики.",
        ]
    elif "THIN_SERVICE_STRUCTURE" in by_code and dom:
        sig_used = signal_label_ru("THIN_SERVICE_STRUCTURE")
        cms_tail = f" (сборка на {cms_label})" if cms_label else ""
        diag_variants = [
            f"Разобрал {dom}{cms_tail}: у практики серьёзная фактура, но ключевые специализации{focus_clause} не разведены в отдельные посадочные разделы, поэтому предметные запросы {clients_word} {in_city} в Яндексе уходят конкурентам.",
            f"Посмотрел структуру {dom}{cms_tail}: направления собраны общим списком без страниц под конкретные категории дел{focus_clause}, из-за чего сайт теряет львиную долю целевой поисковой выдачи {in_city}.",
        ]
    elif ("NO_ONLINE_CAPTURE" in by_code or "NO_MESSENGER_WIDGET" in by_code) and dom:
        code_hit = "NO_MESSENGER_WIDGET" if "NO_MESSENGER_WIDGET" in by_code else "NO_ONLINE_CAPTURE"
        sig_used = signal_label_ru(code_hit)
        cms_tail = f" на {cms_label}" if cms_label else ""
        diag_variants = [
            f"Проверил {dom}{cms_tail}: разделы поданы основательно, но узкие запросы {clients_word}{focus_clause} не вынесены в отдельные посадочные страницы, а с телефона нет быстрого перехода в мессенджер, из-за чего часть обращений {in_city} уходит соседним практикам в выдаче.",
            f"Изучил {dom}{cms_tail} со стороны поисковой выдачи {in_city}: наряду со сжатой структурой страниц{focus_clause} на мобильной версии отсутствует быстрый письменный контакт, где теряется заметная доля первичных обращений {clients_word}.",
        ]
    else:
        cms_tail = f" на {cms_label}" if cms_label else ""
        target_ref = f"{dom}{cms_tail}" if dom else q_cname
        diag_variants = [
            f"Проанализировал видимость {target_ref} в поиске Яндекса {in_city}: по ряду профильных запросов {clients_word}{focus_clause} трафик сейчас перехватывают агрегаторы и практики с выделенными кластерами страниц.",
            f"Сравнил структуру {target_ref} со спросом {in_city}: ключевые направления практики{focus_clause} пока не забирают предметную органическую выдачу Яндекса, хотя потенциал без переплаты за рекламу там высокий.",
        ]
    diag_line = diag_variants[v2 % len(diag_variants)]

    # 3. Прямой оффер (Direct Response) + лёгкий целевой шаг
    free_word = "" if no_free_word else "бесплатно "
    target_asset = dom if dom else f"практики {q_cname}"
    if custom_offer_text:
        offer_line = f"{custom_offer_text}. Прислать сюда?"
    elif want_call:
        offer_line = (
            f"Готов за 10 минут показать на цифрах выдачи {in_city}, какие 2-3 изменения дадут {target_asset} "
            f"приток профильных {clients_word}. Удобно коротко созвониться на этой неделе?"
        )
    else:
        offer_variants = [
            f"Могу {free_word}подготовить и прислать сюда короткий разбор на 1 страницу: где именно {target_asset} теряет спрос в Яндексе {in_city} и какие 3 узла стоит поправить в первую очередь. Прислать?",
            f"Мы уже наметили 3 конкретные точки по {target_asset}, за счёт которых можно забрать целевые обращения из поиска {in_city} с понятным планом работ. Скинуть короткую выжимку сюда в чат?",
            f"Могу {free_word}собрать предметную записку по {target_asset}: реальный поисковый спрос по вашим направлениям {in_city} и пошаговый план структуры без лишнего бюджета. Актуально прислать сюда?",
        ]
        offer_line = offer_variants[v3 % len(offer_variants)]

    if want_short:
        parts = [f"{greet} {pos_line}".strip(), f"{diag_line} {offer_line}".strip()]
        body = "\n\n".join(p for p in parts if p)
    else:
        blocks = [f"{greet} {pos_line}".strip(), diag_line, offer_line]
        body = "\n\n".join(b for b in blocks if b)

    if custom_sig:
        body = f"{body}\n\n{custom_sig}"
    elif sender_name and not want_short:
        body = f"{body}\n\nС уважением, {sender_name}"

    body = sanitize_llm_text(body)

    subject = ""
    if channel == "email":
        subj_variants = [
            f"разбор поискового спроса для {dom}" if dom else f"поисковый спрос по практикам {in_city}",
            f"точки потери обращений на {dom}" if dom else f"разбор выдачи яндекса {in_city}",
            f"структура {dom} под поиск яндекса" if dom else "план структуры сайта под поиск",
        ]
        subject = sanitize_llm_text(subj_variants[v1 % len(subj_variants)].lower().replace("«", "").replace("»", "").replace('"', ""))[:75]

    audit_parts: list[str] = []
    for o in [x for x in observations if x.get("signal_code") != "DECISION_MAKER_FOUND"][:2]:
        txt = (o.get("value_text") or "").strip().rstrip(".")
        if txt:
            audit_parts.append(f"{txt}.")
    if company and company.tech_stack:
        audit_parts.append(f"Стек сайта: {', '.join(company.tech_stack[:3])}.")
    if company and company.address and not skip_addr:
        audit_parts.append(f"Адрес: {company.address}.")
    audit_summary = " ".join(audit_parts) or f"Профильная организация ({city or cfg.icp.niche})."

    return {
        "subject": subject,
        "body": body,
        "audit_summary": audit_summary,
        "signal_used": sig_used,
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
    """Разбирает JSON от модели, очищает пунктуацию, проверяет шлюзом `friday.gate` и сохраняет черновик в SQLite."""
    cfg = config or load_config()
    parsed = extract_json_object(raw_output)
    subject = sanitize_llm_text(str(parsed.get("subject") or "")) if channel == "email" else ""
    body = sanitize_llm_text(str(parsed.get("body") or ""))
    audit_summary = sanitize_llm_text(str(parsed.get("audit_summary") or ""))
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
    autonomous_ui: bool = False,
    prefer_browser_llm: bool = False,
    variation_seed: str = "",
) -> dict:
    """Генерирует персональное деловое предложение с автоматическим подбором настроек и учётом замечаний автора."""
    cfg = config or load_config()
    try:
        load_email_rules(cfg)
    except EmailRulesError as exc:
        raise BrainError(f"Ошибка в пользовательских правилах писем (writer.email_rules): {exc}") from exc

    active_brain = brain or Brain(state=state, config=cfg)
    brain_info = await active_brain.inspect_status()

    # 1. Прямая серверная двухпроходная генерация (если в окружении есть API-ключ / Claude CLI / Codex CLI / Ollama)
    if brain_info["direct_inference_ready"]:
        retry_notes = ""
        last_result: dict | None = None
        last_brain_err: Exception | None = None
        for _attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
            pkt = await build_writer_prompt(
                state,
                prospect,
                channel=channel,
                config=cfg,
                retry_feedback=retry_notes,
                variation_seed=variation_seed,
            )
            try:
                raw_text, provider_used = await active_brain.think(
                    pkt["combined_prompt"],
                    task="personal_outreach",
                )
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
            except httpx.HTTPError as net_exc:
                if brain_info["effective_provider"] == "api" or prefer_browser_llm:
                    return {
                        "ok": False,
                        "queued": False,
                        "needs_browser_llm": True,
                        "reason": str(net_exc),
                        "prompt_packet": pkt,
                        "humanizer_prompt_template": build_humanizer_prompt({"subject": "", "body": ""}),
                    }
                last_brain_err = BrainError(f"Сетевая ошибка обращения к LLM API: {net_exc}")
                break
            except BrainError as b_exc:
                last_brain_err = b_exc
                retry_notes = (
                    f"- Предыдущий ответ не удалось разобрать ({b_exc}). "
                    'Верните ТОЛЬКО непустой JSON {"subject": "...", "body": "...", "audit_summary": "...", "signal_used": "..."} без рассуждений.'
                )
                continue

            if last_result["gate_passed"]:
                return last_result
            retry_notes = "\n".join(f"- {n}" for n in last_result.get("gate_notes") or [])

        if last_result is not None:
            return last_result
        if prefer_browser_llm:
            pkt = await build_writer_prompt(
                state,
                prospect,
                channel=channel,
                config=cfg,
                variation_seed=variation_seed,
            )
            return {
                "ok": False,
                "queued": False,
                "needs_browser_llm": True,
                "reason": str(last_brain_err or "api_empty_fallback"),
                "prompt_packet": pkt,
                "humanizer_prompt_template": build_humanizer_prompt({"subject": "", "body": ""}),
            }
        if autonomous_ui:
            composed = await compose_autonomous_harvey_draft(
                state,
                prospect,
                channel=channel,
                config=cfg,
                variation_seed=variation_seed,
            )
            return await finalize_llm_draft(
                state,
                prospect,
                composed,
                channel=channel,
                provider_used="agent",
                config=cfg,
            )
        if last_brain_err is not None:
            raise last_brain_err
        return {"ok": False, "queued": False, "status": "gate_blocked"}

    # 2. Прозрачный вызов реальной LLM прямо из браузера пользователя (0 настроек, 0 ключей, полный учёт замечаний!)
    if prefer_browser_llm:
        pkt = await build_writer_prompt(
            state,
            prospect,
            channel=channel,
            config=cfg,
            variation_seed=variation_seed,
        )
        return {
            "ok": False,
            "queued": False,
            "needs_browser_llm": True,
            "reason": "zero_config_browser_llm",
            "prompt_packet": pkt,
            "humanizer_prompt_template": build_humanizer_prompt({"subject": "", "body": ""}),
        }

    # 3. Офлайн-резерв для UI / тестов без интернета (по правилам Траута, Райса и прямого маркетинга + учёт feedback)
    if autonomous_ui:
        composed = await compose_autonomous_harvey_draft(
            state,
            prospect,
            channel=channel,
            config=cfg,
            variation_seed=variation_seed,
        )
        return await finalize_llm_draft(
            state,
            prospect,
            composed,
            channel=channel,
            provider_used="agent",
            config=cfg,
        )

    # 4. Программный / CLI режим (`friday drafts` / `agent_tasks`): ставит задачу ИИ-агенту рабочей среды
    pkt = await build_writer_prompt(
        state,
        prospect,
        channel=channel,
        config=cfg,
        variation_seed=variation_seed,
    )
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
    autonomous_ui: bool = False,
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
            autonomous_ui=autonomous_ui,
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
