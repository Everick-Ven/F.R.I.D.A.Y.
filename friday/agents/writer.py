"""Агент-автор (Writer) — самонастраивающаяся схема Harvey для персональных B2B-писем.

Ключевые принципы:
- Пользователь НЕ выбирает вручную модели, провайдеров, температуру или режим Humanizer.
- `Brain` автоматически выбирает сильнейший доступный движок (`api` из окружения -> `claude` CLI ->
  `codex` CLI -> локальный `ollama` -> ИИ-агент рабочей среды), включает двухпроходный `Humanizer`
  и проверяет каждый черновик детерминированным шлюзом `friday.gate.validate_draft` (ФЗ-38 + КПЭА + анти-ИИ).
- Первое касание строго следует философии Harvey: до 70–75 слов, без рекламной выгрузки услуг «в лоб»,
  без длинных тире (`—`), одно проверяемое наблюдение о бизнесе получателя и один живой вопрос.
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
    "- До 70 слов (включая приветствие). Одно конкретное проверяемое наблюдение "
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
    if (prospect.role_kind or "").strip() in ("company_inbox", "company_contact"):
        return True
    if not (prospect.first_name or "").strip():
        return True
    if company and prospect.full_name.strip().lower() == (company.name or "").strip().lower():
        return True
    return False


def _auto_address_hint(cfg: FridayConfig, prospect: Prospect) -> str:
    """Сам выбирает уместный регистр обращения по отрасли и должности ЛПР."""
    conservative_niche = cfg.compliance.regulated_niche in ("legal_advocates", "medicine", "finance")
    senior_title = any(
        w in (prospect.title or "").lower()
        for w in ("председатель", "президент", "управляющ", "партнёр", "партнер", "генеральн", "главврач")
    )
    if cfg.persona.address_style == "formal_vy" or conservative_niche or senior_title:
        return "строго на «Вы» по Имени и Отчеству (если отчество известно)"
    return "уважительно на «Вы» по имени (или Имени Отчеству при указанном отчестве)"


async def build_writer_prompt(
    state: StateManager,
    prospect: Prospect,
    *,
    channel: str = "telegram",
    config: FridayConfig | None = None,
    retry_feedback: str = "",
) -> dict:
    """Собирает полный промпт Harvey `Writer` с автоматическим выбором лучших настроек тона и структуры."""
    cfg = config or load_config()
    company = await state.get_company(prospect.company_id) if prospect.company_id else None
    observations = await state.get_observations(company_id=prospect.company_id) if prospect.company_id else []

    rules = load_email_rules(cfg)
    address_hint = _auto_address_hint(cfg, prospect)
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


async def compose_autonomous_harvey_draft(
    state: StateManager,
    prospect: Prospect,
    *,
    channel: str = "telegram",
    config: FridayConfig | None = None,
    variation_seed: str = "",
) -> dict:
    """Автономная вариативная композиция по правилам Harvey при работе в изолированной песочнице без внешних LLM:
    - никакого питча услуг в первом касании (только наблюдение по факту + один предметный вопрос);
    - учёт сохранённых замечаний владельца (`writing_feedback`);
    - разные заходы, ритм и вопросы для каждой карточки (и при повторном нажатии «Переписать»);
    - обязательное прохождение `friday.gate.validate_draft` (0 длинных тире, 0 штампов, до 65 слов).
    """
    cfg = config or load_config()
    company = await state.get_company(prospect.company_id) if prospect.company_id else None
    observations = await state.get_observations(company_id=prospect.company_id) if prospect.company_id else []
    feedback_notes = await state.get_writing_feedback([prospect.id], limit=5)
    fb_joined = " ".join(str(f.get("comment") or "") for f in feedback_notes).lower()

    want_short = any(k in fb_joined for k in ("короч", "кратк", "2 предлож", "два предлож", "сжат"))
    skip_addr = any(k in fb_joined for k in ("без адрес", "не упоминай адрес", "не пиши адрес"))
    skip_stack = any(k in fb_joined for k in ("без tilda", "без тильд", "без cms", "без стек", "не упоминай tilda"))

    custom_question = ""
    for f in feedback_notes:
        cm = str(f.get("comment") or "").strip()
        q_match = re.search(r"([А-Яа-яЁё][^?.!]{8,140}\?)", cm)
        if q_match:
            custom_question = q_match.group(1).strip().replace("—", ",").replace("!", "")
            break

    h_bytes = hashlib.sha256(f"{prospect.id}:{company.id if company else ''}:{variation_seed}".encode("utf-8")).digest()
    v1 = h_bytes[0]
    v2 = h_bytes[1]

    general_inbox = _is_general_inbox(prospect, company)
    if general_inbox:
        greetings = ["Здравствуйте.", "Коллеги, здравствуйте.", "Добрый день."]
        greet = greetings[v1 % len(greetings)]
    else:
        pg = (prospect.polite_greeting or prospect.first_name or "").strip()
        greetings = [f"{pg}, здравствуйте.", f"{pg}, добрый день."]
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
    in_city = _loc_map.get(city.lower(), f"по городу {city}" if city else "")
    stack = [s for s in (company.tech_stack if company else []) if s]
    cms_label = stack[0] if (stack and not skip_stack) else ""
    addr_short = ""
    if company and company.address and not skip_addr:
        parts = [p.strip() for p in company.address.split(",") if p.strip() and p.strip().lower() != city.lower()]
        if parts:
            addr_short = parts[0]

    by_code = {o["signal_code"]: o for o in observations if o.get("signal_code") != "DECISION_MAKER_FOUND"}
    is_legal = cfg.compliance.regulated_niche == "legal_advocates" or "адвокат" in (cfg.icp.niche or "").lower()
    clients_word = "доверителей" if is_legal else "клиентов"

    sig_used = "Анализ открытого профиля"
    if "HH_HIRING_ROLE" in by_code:
        raw_vac = (by_code["HH_HIRING_ROLE"].get("value_text") or "профильного специалиста").replace("Вакансия:", "").strip(" «».")
        sig_used = signal_label_ru("HH_HIRING_ROLE")
        obs_variants = [
            f"Увидел у {q_cname} открытую вакансию ({raw_vac}) на hh.ru.",
            f"Обратил внимание на подбор в {q_cname} на hh.ru ({raw_vac}).",
        ]
        q_variants = [
            f"Подскажите, расширение команды сейчас поддерживаете потоком обращений из поиска Яндекса или опираетесь на рекомендации?",
            f"Любопытно, под рост команды вы сейчас перестраиваете привлечение {clients_word} с сайта или входящих уже хватает?",
        ]
    elif "NO_WEBSITE_ON_MAP" in by_code or (not dom and company and (company.ymaps_url or addr_short)):
        sig_used = signal_label_ru("NO_WEBSITE_ON_MAP")
        loc_phrase = f" ({addr_short})" if addr_short else (f" {in_city}" if in_city else "")
        obs_variants = [
            f"Нашёл карточку {q_cname} в Яндекс.Картах{loc_phrase}, при этом отдельный сайт там не привязан.",
            f"Изучал профильные практики{f' {in_city}' if in_city else ''} и увидел карточку {q_cname}{loc_phrase} без ссылки на сайт.",
            f"Наткнулся на профиль {q_cname} в геосервисах{loc_phrase}, где указан телефон, но нет сайта с описанием направлений.",
        ]
        q_variants = [
            f"Подскажите, вся загрузка сейчас закрывается личными рекомендациями, или выход в поиск Яндекса тоже в планах?",
            f"Интересно, геосервисов и сарафана сейчас достаточно для загрузки практики, или отдельный сайт планируете?",
            f"Скажите, вы осознанно не ведёте отдельный сайт под поисковые запросы {clients_word}, или просто не доходили руки?",
        ]
    elif "THIN_SERVICE_STRUCTURE" in by_code and dom:
        sig_used = signal_label_ru("THIN_SERVICE_STRUCTURE")
        cms_tail = f" на {cms_label}" if cms_label else ""
        obs_variants = [
            f"Зашёл на {dom}{cms_tail}: ключевые направления собраны компактно, но под узкие поисковые запросы {clients_word} отдельных страниц почти нет.",
            f"Изучал {dom}{cms_tail} и заметил, что профильные специализации объединены без отдельных посадочных разделов под поиск Яндекса.",
            f"Открывал {dom}{cms_tail}: сильная фактура практики, но в структуре сайта направления не разведены под конкретные запросы в поиске.",
        ]
        q_variants = [
            f"Подскажите, основной поток {clients_word} сейчас идёт по рекомендациям или органику Яндекса тоже задействуете?",
            f"Интересно, сайт сейчас держите скорее как визитку для тех, кто уже пришёл по рекомендации, или ставите задачу расти в поиске?",
            f"Скажите, вы целенаправленно держите лаконичную структуру {dom} или планировали расширять разделы под поиск?",
        ]
    elif ("NO_ONLINE_CAPTURE" in by_code or "NO_MESSENGER_WIDGET" in by_code) and dom:
        code_hit = "NO_MESSENGER_WIDGET" if "NO_MESSENGER_WIDGET" in by_code else "NO_ONLINE_CAPTURE"
        sig_used = signal_label_ru(code_hit)
        obs_variants = [
            f"Открывал {dom} со смартфона: телефоны видны сразу, но быстрого перехода в мессенджер для тех, кому неудобно звонить днём, не нашёл.",
            f"Заглянул на {dom}: контакты указаны, однако короткой формы или кнопки Telegram для быстрых обращений на страницах нет.",
            f"Смотрел {dom} глазами нового посетителя: при переходе с телефона связаться можно только звонком, без быстрого чата.",
        ]
        q_variants = [
            f"Подскажите, новые обращения у вас чаще идут сразу по телефону или люди всё чаще пишут в Telegram и WhatsApp?",
            f"Любопытно, основная часть первичных обращений к вам приходит звонками в приёмную или через мессенджеры?",
            f"Скажите, вам комфортнее принимать первичные запросы только голосом по телефону или письменные обращения тоже удобны?",
        ]
    elif "TELEGRAM_PUBLIC" in by_code and dom:
        sig_used = signal_label_ru("TELEGRAM_PUBLIC")
        cms_tail = f" на {cms_label}" if cms_label else ""
        obs_variants = [
            f"Увидел на {dom}{cms_tail} связку сайта с вашим публичным Telegram-каналом.",
            f"Обратил внимание, что на {dom}{cms_tail} вы ведёте аудиторию параллельно в Telegram-канал.",
        ]
        q_variants = [
            f"Любопытно, связка сайта и канала уже приводит новых {clients_word} из поиска или работает для текущей аудитории?",
            f"Подскажите, органический поиск Яндекса даёт заметный вклад в обращения наряду с Telegram и рекомендациями?",
        ]
    elif "NO_ANALYTICS" in by_code and dom:
        sig_used = signal_label_ru("NO_ANALYTICS")
        obs_variants = [
            f"Посмотрел {dom}: у практики сильная фактура, при этом счётчиков веб-аналитики на страницах не увидел.",
            f"Заглянул на {dom} и заметил, что на сайте не подключена Яндекс.Метрика для оценки поисковых обращений.",
        ]
        q_variants = [
            f"Подскажите, обращения с поиска сейчас не замеряете целенаправленно, потому что поток идёт по рекомендациям?",
            f"Любопытно, вы сейчас опираетесь в основном на рекомендации или органический поиск Яндекса тоже в фокусе?",
        ]
    elif dom:
        cms_tail = f" на {cms_label}" if cms_label else ""
        sig_used = signal_label_ru("TECH_STACK_RU") if cms_label else "Профиль сайта организации"
        obs_variants = [
            f"Изучал профильные практики{f' {in_city}' if in_city else ''} и внимательно посмотрел {dom}{cms_tail}.",
            f"Смотрел выдачу по ключевым направлениям{f' {in_city}' if in_city else ''} и заглянул на {dom}{cms_tail}.",
            f"Познакомился со структурой сайта {q_cname} ({dom}){cms_tail}.",
        ]
        q_variants = [
            f"Подскажите, органический поиск Яндекса сейчас даёт вам целевые обращения или основная загрузка идёт по рекомендациям?",
            f"Интересно, вы сейчас активно развиваете {dom} под поисковый спрос или текущих обращений по сарафану достаточно?",
            f"Скажите, привлечение профильных {clients_word} из поиска Яндекса для вас в этом году в приоритете?",
        ]
    else:
        obs_variants = [
            f"Изучал профильные практики{f' {in_city}' if in_city else ''} и обратил внимание на {q_cname}.",
            f"Познакомился с открытым профилем {q_cname}{f' ({city})' if city else ''}.",
        ]
        q_variants = [
            f"Подскажите, новые {clients_word} к вам сейчас приходят в основном по рекомендациям или поиск Яндекса тоже работает?",
            f"Интересно, рассматриваете ли вы сейчас усиление видимости практики в поиске Яндекса или загрузка полная?",
        ]

    obs_line = obs_variants[v1 % len(obs_variants)].replace("—", ",").replace("!", ".")
    q_line = custom_question or q_variants[v2 % len(q_variants)].replace("—", ",").replace("!", "")

    if want_short:
        body = f"{greet} {obs_line} {q_line}"
    else:
        sep = "\n\n" if channel == "email" else " "
        body = f"{greet} {obs_line}{sep}{q_line}"

    subject = ""
    if channel == "email":
        subj_variants = [
            f"вопрос по сайту {dom}" if dom else f"вопрос для {cname.lower()[:28]}",
            f"поиск яндекса и {dom}" if dom else "вопрос по практике",
            f"структура {dom}" if dom else "короткий вопрос",
        ]
        subject = subj_variants[v1 % len(subj_variants)].lower().replace("«", "").replace("»", "").replace('"', "")[:70]

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
    variation_seed: str = "",
) -> dict:
    """Генерирует персональное письмо с автоматическим подбором оптимальных настроек Мозга."""
    cfg = config or load_config()
    try:
        load_email_rules(cfg)
    except EmailRulesError as exc:
        raise BrainError(f"Ошибка в пользовательских правилах писем (writer.email_rules): {exc}") from exc

    active_brain = brain or Brain(state=state, config=cfg)
    brain_info = await active_brain.inspect_status()

    # 1. Прямая двухпроходная генерация (Writer + авто-Humanizer + самокоррекция по Gate)
    if brain_info["direct_inference_ready"]:
        retry_notes = ""
        last_result: dict | None = None
        for _attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
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
                if brain_info["effective_provider"] == "api" and brain_info["has_api_key"]:
                    return {
                        "ok": False,
                        "queued": False,
                        "needs_browser_llm": True,
                        "reason": str(net_exc),
                        "prompt_packet": pkt,
                        "humanizer_prompt_template": build_humanizer_prompt({"subject": "", "body": ""}),
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

    # 2. Если вызов идёт из интерактивной кнопки UI без локальных CLI/ключей — никаких модалок и затыков:
    #    система сама строит вариативное письмо по правилам Harvey и учитывает все замечания обратной связи.
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

    # 3. Программный / CLI режим (`friday drafts` / `agent_tasks`): ставит задачу ИИ-агенту рабочей среды
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
