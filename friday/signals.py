"""Управляемый словарь сигналов F.R.I.D.A.Y. для российского юридического рынка.

Ключевой принцип архитектуры (унаследованный от Harvey):
- Каждый факт о практике или адвокате хранится как строка-наблюдение (`observations`),
  а не как жёсткая колонка таблицы.
- Новый сигнал не требует миграции БД, повторный сбор создаёт временной ряд,
  а вместе с фактом всегда сохраняется `evidence_url` и уверенность (`confidence`).
- F.R.I.D.A.Y. предлагает каталог сигналов со статусом `proposed`; в сбор идут
  только те сигналы, которые человек явно подтвердил (`status = 'confirmed'`).
"""

from __future__ import annotations

SIGNAL_CATALOG: list[dict] = [
    # ── DISCOVERY: поисковая выдача Яндекса, карты и реестры ──
    {
        "code": "YANDEX_SERP_RANK",
        "label": "Позиция в органической выдаче Яндекса",
        "description": (
            "Позиция сайта в поиске Яндекса по запросу «<практика> <город>» (фраза сохраняется "
            "вместе с наблюдением). Позиции 11–30 — главная точка роста: практика уже пытается "
            "привлекать доверителей из поиска, но находится за пределами первой страницы."
        ),
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "платно при использовании Yandex Search API / XML (~0.1–0.5 ₽ за запрос)",
        "is_free": False,
        "confidence_floor": 0.0,
    },
    {
        "code": "FOUND_IN_YANDEX",
        "label": "Найден в поиске по целевому запросу",
        "description": "По какому именно юридическому запросу и городу практика попала в выборку.",
        "category": "discovery",
        "value_type": "text",
        "collector": "discover",
        "cost_note": "включено в поисковый запрос",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "YANDEX_MAPS_RATING",
        "label": "Рейтинг карточки в Яндекс Картах",
        "description": (
            "Средняя оценка организации в Яндекс Картах. Высокий рейтинг при малом числе отзывов "
            "или без сайта означает хорошую репутацию при слабой упаковке трафика."
        ),
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "включено в запрос Яндекс Карт / импорт",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "YANDEX_MAPS_REVIEWS",
        "label": "Количество оценок/отзывов в Яндекс Картах",
        "description": "Число отзывов в карточке Яндекс Карт — индикатор локальной видимости практики.",
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "включено в запрос Яндекс Карт / импорт",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "TWOGIS_RATING",
        "label": "Рейтинг карточки в 2ГИС",
        "description": "Средний рейтинг адвокатского образования или юрфирмы в справочнике 2ГИС.",
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "включено в запрос 2ГИС API / импорт",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "TWOGIS_REVIEWS",
        "label": "Количество отзывов в 2ГИС",
        "description": "Число отзывов в карточке 2ГИС (в городах-миллионниках РФ — один из главных источников локального трафика).",
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "включено в запрос 2ГИС API / импорт",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "NO_WEBSITE",
        "label": "Нет собственного сайта",
        "description": (
            "У адвокатского образования есть запись в 2ГИС, Яндекс Картах или реестре, указан телефон, "
            "но отсутствует собственный сайт."
        ),
        "category": "discovery",
        "value_type": "bool",
        "collector": "discover",
        "cost_note": "бесплатно (из карточки организации или реестра)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "UNCLAIMED_OR_EMPTY_CARD",
        "label": "Незаполненная карточка в Яндекс Картах / 2ГИС",
        "description": (
            "В карточке справочника отсутствуют ключевые поля (не привязан сайт, нет графика работы, "
            "мессенджеров или профильных рубрик) — локальный спрос теряется."
        ),
        "category": "discovery",
        "value_type": "bool",
        "collector": "discover",
        "cost_note": "бесплатно (из карточки организации)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "MINJUST_REGISTRY_ACTIVE",
        "label": "Действующий статус в реестре адвокатов РФ",
        "description": (
            "Подтверждён реестровый номер адвоката (формат СС/NNNNN) и действующий статус "
            "по данным реестра Минюста РФ / Адвокатской палаты субъекта."
        ),
        "category": "discovery",
        "value_type": "text",
        "collector": "discover",
        "cost_note": "бесплатно (открытые данные реестров / сайта)",
        "is_free": True,
        "confidence_floor": 0.0,
    },

    # ── PROFILE: разбор сайта практики ($0 — прямой HTTP без LLM) ──
    {
        "code": "NO_YANDEX_METRIKA",
        "label": "Отсутствует счётчик Яндекс.Метрики",
        "description": (
            "На главной странице не обнаружен код Яндекс.Метрики (mc.yandex.ru/metrika). "
            "Практика не отслеживает конверсию заявок и поисковые запросы доверителей."
        ),
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "бесплатно (HTML главной страницы)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "RUNNING_YANDEX_DIRECT",
        "label": "Признаки платного трафика / коллтрекинга (Яндекс.Директ, Calltouch, Roistat)",
        "description": (
            "На сайте установлены скрипты РСЯ/Яндекс.Директа, Calltouch, Roistat, CoMagic, Yagla или Marquiz — "
            "практика уже инвестирует бюджет в платное привлечение доверителей."
        ),
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "бесплатно (HTML главной страницы)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "TECH_STACK_RU",
        "label": "Платформа сайта, CRM и виджеты",
        "description": (
            "Обнаруженный стек российского сайта: Tilda, 1С-Битрикс, WordPress, Яндекс.Метрика, "
            "Битрикс24, amoCRM, Jivo, Calltouch, Roistat, Marquiz и др."
        ),
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "бесплатно (HTML главной страницы)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "MISSING_PRACTICE_PAGES",
        "label": "Нет отдельных посадочных страниц по специализациям",
        "description": (
            "На сайте не найдены отдельные разделы под конкретные категории дел (уголовные, семейные, "
            "арбитраж, банкротство, наследство и т.д.) — всё сведено на одну общую страницу, "
            "из-за чего сайт теряет органический трафик в Яндексе."
        ),
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "бесплатно (навигация сайта + sitemap.xml)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "WEAK_TRUST_TRANSPARENCY",
        "label": "Не указаны реестровые номера / форма адвокатского образования / практика",
        "description": (
            "На сайте отсутствуют явные маркеры прозрачности для осторожного доверителя: "
            "реестровый номер в палате, точная форма образования (коллегия/бюро/кабинет) или раздел судебной практики."
        ),
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "бесплатно (HTML сайта)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "COMPLIANCE_RISK_ON_SITE",
        "label": "Рискованные формулировки по 38-ФЗ «О рекламе» или ст. 17 КПЭА на сайте",
        "description": (
            "На сайте самой практики встретились триггеры риска (обещания «100% гарантии выигрыша», "
            "«лучший адвокат», блок клиентских отзывов на сайте адвокатского образования), "
            "которые мешают модерации в Яндекс.Директе и несут дисциплинарные риски по КПЭА."
        ),
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "бесплатно (HTML сайта)",
        "is_free": True,
        "confidence_floor": 0.7,
    },
    {
        "code": "NO_FAST_MESSENGER_CTA",
        "label": "Нет быстрого перехода в Telegram / WhatsApp",
        "description": (
            "На сайте нет прямой кнопки быстрой связи в мессенджере — доверители со смартфона "
            "вынуждены звонить или заполнять длинную форму."
        ),
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "бесплатно (HTML главной страницы)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "NO_SCHEMA_MARKUP",
        "label": "Отсутствует микроразметка Schema.org",
        "description": "Нет JSON-LD / Schema.org разметки (LegalService / Attorney / Organization) для поисковых сниппетов Яндекса.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "бесплатно (HTML главной страницы)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "NO_SSL_OR_MOBILE_ISSUES",
        "label": "Проблемы с HTTPS или мобильной адаптацией (viewport)",
        "description": "Сайт открывается без SSL или не содержит meta viewport — критично для мобильной выдачи Яндекса.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "бесплатно (HTTP-запрос)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "SITE_PAGE_COUNT",
        "label": "Размер сайта (число страниц в sitemap.xml)",
        "description": "Количество страниц в карте сайта: 1–3 страницы говорят о сайте-визитке без SEO-охвата.",
        "category": "profile",
        "value_type": "num",
        "collector": "profile",
        "cost_note": "бесплатно (sitemap.xml)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "BLOG_STALE",
        "label": "Давно не обновлялись публикации / практика (дней)",
        "description": "Сколько дней назад в sitemap.xml обновлялись материалы (признак заброшенного контент-маркетинга).",
        "category": "profile",
        "value_type": "num",
        "collector": "profile",
        "cost_note": "бесплатно (sitemap.xml)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "INCUMBENT_AGENCY",
        "label": "Текущий подрядчик по сайту / маркетингу в подвале",
        "description": "В подвале сайта явно указано агентство («Разработка / продвижение сайта — ...»).",
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "бесплатно (подвал сайта)",
        "is_free": True,
        "confidence_floor": 0.75,
    },

    # ── PEOPLE & CONTACTS: кто принимает решение и как связаться вручную ──
    {
        "code": "DECISION_MAKER_FOUND",
        "label": "Найден ЛПР (управляющий партнёр / председатель / адвокат)",
        "description": "На сайте определены ФИО и должность руководителя адвокатского образования или профильного партнёра.",
        "category": "people",
        "value_type": "text",
        "collector": "people",
        "cost_note": "бесплатно (страницы «О коллегии / Команда / Адвокаты»)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "TELEGRAM_CONTACT",
        "label": "Публичный контакт в Telegram",
        "description": "На сайте или в карточке указан публичный Telegram для связи или канал практики.",
        "category": "people",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "бесплатно (HTML сайта / карточка)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "PUBLIC_EMAIL_CONTACT",
        "label": "Опубликованный рабочий email",
        "description": "Публично размещённый на сайте или в реестре адрес электронной почты (без угадывания чужих адресов).",
        "category": "people",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "бесплатно (HTML сайта / карточка)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
    {
        "code": "CONTACT_FORM_URL",
        "label": "Страница контактов / форма обратной связи",
        "description": "URL страницы контактов или формы обращения на сайте практики.",
        "category": "people",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "бесплатно (HTML сайта)",
        "is_free": True,
        "confidence_floor": 0.0,
    },
]

FREE_SIGNAL_CODES: frozenset[str] = frozenset(
    sig["code"] for sig in SIGNAL_CATALOG if sig.get("is_free", True)
)
ALL_SIGNAL_CODES: frozenset[str] = frozenset(sig["code"] for sig in SIGNAL_CATALOG)


async def seed_signal_catalog(state, auto_confirm_free: bool = False) -> int:
    """Зарегистрировать словарь сигналов в БД без перезаписи пользовательских решений."""
    for sig in SIGNAL_CATALOG:
        await state.upsert_signal_code(
            sig["code"],
            label=sig["label"],
            description=sig["description"],
            category=sig["category"],
            value_type=sig["value_type"],
            collector=sig["collector"],
            cost_note=sig["cost_note"],
            confidence_floor=sig.get("confidence_floor", 0.0),
        )
    if auto_confirm_free:
        for code in FREE_SIGNAL_CODES:
            cur = await state.get_signal_code(code)
            if cur and cur.get("status") == "proposed":
                await state.set_signal_status(code, "confirmed")
    return len(SIGNAL_CATALOG)
