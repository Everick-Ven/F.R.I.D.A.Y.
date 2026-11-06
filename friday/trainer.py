"""Настройка проекта под продукт пользователя и целевую нишу рынка РФ."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

from friday.config import FridayConfig, load_config, save_local_config
from friday.gate import display_domain
from friday.paths import LOCAL_CONFIG_PATH, SKILLS_DIR
from friday.public_http import PublicHttpError, SafePublicHttpClient
from friday.untrusted import UNTRUSTED_RULE, wrap_untrusted

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.IGNORECASE | re.DOTALL)
LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)

KEY_SUBPAGE_HINTS = (
    "about", "o-kompanii", "o-nas", "company",
    "product", "services", "uslugi", "solutions", "resheniya",
    "pricing", "tarify", "price", "tseny", "cases", "kejsy",
)

NICHE_TEMPLATES_RU: list[dict] = [
    {
        "id": "b2b_crm_integrator",
        "short_label": "IT и CRM",
        "label": "Внедрение CRM и сквозной аналитики",
        "regulated_niche": "none",
        "persona": {
            "name": "Алексей Смирнов",
            "company": "КонтурПоток",
            "role": "Руководитель проектов",
            "telegram": "@konturpotok",
            "website": "https://example-crm.ru",
        },
        "product": {
            "name": "Внедрение amoCRM / Битрикс24 и сквозной аналитики",
            "category": "Автоматизация продаж",
            "description": (
                "Настраиваем связку сайта, телефонии, мессенджеров и CRM за 14 дней, "
                "чтобы обращения с рекламы и карт не терялись между менеджерами."
            ),
            "pricing": "от 65 000 ₽ за проект",
            "key_benefits": [
                "Автоматическая фиксация всех звонков, форм и чатов в CRM",
                "Прозрачная воронка продаж без ручных таблиц",
            ],
            "offer": {
                "entry": "прислать короткий разбор пути заявки на вашем сайте из 3 пунктов",
                "goal": "send_audit",
            },
        },
        "icp": {
            "niche": "Производственные и оптовые компании с отделом продаж",
            "industries": ["Производство", "Оптовая торговля", "Строительные услуги"],
            "cities": ["Москва", "Санкт-Петербург", "Екатеринбург", "Казань"],
            "search_queries": [
                "производство металлоконструкций",
                "оптовая поставка стройматериалов",
                "промышленное оборудование поставка",
            ],
            "decision_maker_titles": [
                "коммерческий директор",
                "руководитель отдела продаж",
                "генеральный директор",
                "основатель",
            ],
            "hiring_signals": [
                "менеджер по продажам",
                "руководитель отдела продаж",
            ],
        },
    },
    {
        "id": "marketing_seo_ru",
        "short_label": "Маркетинг и SEO",
        "label": "Разработка сайтов, SEO и геомаркетинг",
        "regulated_niche": "none",
        "persona": {
            "name": "Мария Власова",
            "company": "ГеоРост Digital",
            "role": "Руководитель поискового направления",
            "telegram": "@georost",
            "website": "https://example-agency.ru",
        },
        "product": {
            "name": "Поисковое продвижение в Яндексе и геосервисах",
            "category": "Поисковый маркетинг и разработка",
            "description": (
                "Создаём посадочные страницы под структуру спроса и выводим компании "
                "в топ органической выдачи Яндекса и карточек 2ГИС / Яндекс Карт."
            ),
            "pricing": "от 55 000 ₽/мес",
            "key_benefits": [
                "Рост целевых обращений из поиска без перегрева ставок в рекламе",
                "Понятный поэтапный план работ под приоритетные услуги",
            ],
            "offer": {
                "entry": "прислать короткий разбор из 3 конкретных пунктов по вашему сайту и поисковой выдаче",
                "goal": "send_audit",
            },
        },
        "icp": {
            "niche": "Частные клиники, профильные центры и сервисные компании",
            "industries": ["Локальные услуги", "Частные клиники", "Автобизнес"],
            "cities": ["Москва", "Санкт-Петербург", "Краснодар", "Нижний Новгород"],
            "search_queries": [
                "стоматологическая клиника",
                "кузовной ремонт автосервис",
                "центр косметологии",
            ],
            "decision_maker_titles": [
                "директор по маркетингу",
                "управляющий",
                "основатель",
                "генеральный директор",
            ],
            "hiring_signals": [
                "маркетолог",
                "директолог",
            ],
        },
    },
    {
        "id": "b2b_logistics_ved",
        "short_label": "Логистика и ВЭД",
        "label": "Международная логистика и таможенное оформление",
        "regulated_niche": "none",
        "persona": {
            "name": "Дмитрий Орлов",
            "company": "ВостокТранс ВЭД",
            "role": "Директор по работе с корпоративными клиентами",
            "telegram": "@vostoktrans",
            "website": "https://example-ved.ru",
        },
        "product": {
            "name": "Международная логистика и белое таможенное оформление",
            "category": "Логистика и ВЭД",
            "description": (
                "Организуем прямые поставки комплектующих и оборудования с прозрачными расчётами "
                "и полным комплектом закрывающих документов и ГТД."
            ),
            "pricing": "расчёт маршрута за 2 часа",
            "key_benefits": [
                "Прогнозируемые сроки прохождения границы и таможни",
                "Страхование груза на всём маршруте",
            ],
            "offer": {
                "entry": "сделать расчёт альтернативного маршрута и ставки на одной типовой партии",
                "goal": "get_reply",
            },
        },
        "icp": {
            "niche": "Торговые дома, дистрибьюторы и импортёры оборудования",
            "industries": ["Дистрибуция", "Импорт оборудования", "Комплектующие"],
            "cities": ["Москва", "Санкт-Петербург", "Владивосток", "Екатеринбург"],
            "search_queries": [
                "оптовый поставщик промышленной фурнитуры",
                "дистрибьютор электротехнического оборудования",
                "поставка станков и комплектующих",
            ],
            "decision_maker_titles": [
                "директор по закупкам",
                "руководитель отдела логистики",
                "руководитель ВЭД",
                "генеральный директор",
            ],
            "hiring_signals": [
                "менеджер по закупкам",
                "менеджер ВЭД",
                "логист",
            ],
        },
    },
    {
        "id": "horeca_retail_supply",
        "short_label": "HoReCa и ритейл",
        "label": "Поставки и сервис для ресторанов и кофеен",
        "regulated_niche": "none",
        "persona": {
            "name": "Екатерина Морозова",
            "company": "ПрофХорека",
            "role": "Руководитель отдела HoReCa",
            "telegram": "@profhoreca",
            "website": "https://example-horeca.ru",
        },
        "product": {
            "name": "Поставка ингредиентов, оборудования и сервис 24/7 для заведений",
            "category": "Поставки для HoReCa",
            "description": (
                "Бесперебойные поставки для кофеен, пекарен и ресторанов с отсрочкой платежа "
                "и подменным оборудованием в день обращения."
            ),
            "pricing": "оптовый прайс от 15 000 ₽",
            "key_benefits": [
                "Подменное оборудование в день заявки без простоя точки",
                "Стабильная матрица позиций под сетевые стандарты",
            ],
            "offer": {
                "entry": "отправить актуальный оптовый прайс и привезти дегустационный сет",
                "goal": "get_reply",
            },
        },
        "icp": {
            "niche": "Рестораны, сети кофеен, пекарни и отели",
            "industries": ["Рестораны и кафе", "Кофейни и пекарни", "Гостиничный бизнес"],
            "cities": ["Москва", "Санкт-Петербург", "Казань", "Сочи"],
            "search_queries": [
                "сеть кофеен",
                "ресторан авторской кухни",
                "ремесленная пекарня",
            ],
            "decision_maker_titles": [
                "управляющий рестораном",
                "шеф-бариста",
                "директор по закупкам",
                "владелец",
            ],
            "hiring_signals": [
                "управляющий рестораном",
                "шеф-повар",
                "бариста",
            ],
        },
    },
]


def _clean_html_text(html: str, max_chars: int = 4000) -> str:
    cleaned = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


async def crawl_product_site(website_url: str, *, timeout: float = 5.0) -> dict:
    """Безопасно скачивает главную и до 2 ключевых подстраниц сайта продукта пользователя."""
    url = website_url.strip()
    if not url:
        return {"url": "", "pages": [], "error": ""}
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    pages: list[dict] = []
    client = SafePublicHttpClient(timeout=timeout)
    try:
        resp = await client.get(url)
        html = resp.text[:250_000]
        t_m = TITLE_RE.search(html)
        d_m = META_DESC_RE.search(html)
        headings = [
            re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
            for m in HEADING_RE.finditer(html)
        ]
        headings = [h for h in headings if 3 <= len(h) <= 140][:12]
        pages.append(
            {
                "url": str(resp.url),
                "title": re.sub(r"\s+", " ", unescape(t_m.group(1))).strip()[:200] if t_m else "",
                "meta_description": unescape(d_m.group(1)).strip()[:300] if d_m else "",
                "headings": headings,
                "excerpt": _clean_html_text(html, max_chars=3500),
            }
        )

        base_host = (urlparse(str(resp.url)).hostname or "").lower().removeprefix("www.")
        sub_urls: list[str] = []
        for m in LINK_RE.finditer(html):
            href = m.group(1).strip()
            full = urljoin(str(resp.url), href)
            parsed = urlparse(full)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            if host != base_host:
                continue
            path_l = (parsed.path or "").lower()
            if any(hint in path_l for hint in KEY_SUBPAGE_HINTS) and full not in sub_urls and full != str(resp.url):
                sub_urls.append(full)
            if len(sub_urls) >= 2:
                break

        for sub in sub_urls:
            try:
                sub_resp = await client.get(sub)
                if sub_resp.status_code < 400:
                    sub_html = sub_resp.text[:180_000]
                    st_m = TITLE_RE.search(sub_html)
                    pages.append(
                        {
                            "url": str(sub_resp.url),
                            "title": re.sub(r"\s+", " ", unescape(st_m.group(1))).strip()[:200] if st_m else "",
                            "meta_description": "",
                            "headings": [],
                            "excerpt": _clean_html_text(sub_html, max_chars=2500),
                        }
                    )
            except PublicHttpError:
                continue

        return {"url": url, "pages": pages, "error": ""}
    except PublicHttpError as exc:
        return {"url": url, "pages": pages, "error": str(exc)}
    finally:
        await client.close()


def _brand_from_url_or_crawl(website: str, crawl: dict) -> str:
    pages = crawl.get("pages") or []
    if pages and pages[0].get("title"):
        raw_title = pages[0]["title"]
        # Берём первую осмысленную часть до разделителя «— | :»
        parts = [p.strip() for p in re.split(r"[\|—–:]", raw_title) if p.strip()]
        if parts and 2 <= len(parts[0]) <= 42:
            return parts[0]
    dom = display_domain(website)
    if dom:
        stem = dom.split(".")[0].replace("-", " ").replace("_", " ").strip()
        if stem:
            return stem.title() if stem.isascii() else stem.capitalize()
    return "Наш проект"


def synthesize_niche_profile(
    *,
    website: str = "",
    crawl: dict | None = None,
    brief: str = "",
    niche_hint: str = "",
    cities: list[str] | None = None,
) -> dict:
    """Формирует полный профиль проекта и ICP по сайту, брифу и целевой нише."""
    crawl = crawl or {}
    pages = crawl.get("pages") or []
    site_meta = pages[0].get("meta_description", "") if pages else ""
    site_headings = " ".join(pages[0].get("headings") or []) if pages else ""

    clean_url = website.strip()
    if clean_url and not clean_url.startswith(("http://", "https://")):
        clean_url = f"https://{clean_url}"

    company_name = _brand_from_url_or_crawl(clean_url, crawl)
    combined_text = f"{niche_hint} {brief} {site_meta} {site_headings}".lower()

    # 1. Определяем регулируемую отрасль ФЗ-38 / КПЭА
    regulated_niche = "none"
    if any(k in combined_text for k in ("адвокат", "адвокатск", "коллеги", "кпэа")):
        regulated_niche = "legal_advocates"
    elif any(k in combined_text for k in ("клиник", "стоматолог", "медицин", "врач", "косметолог")):
        regulated_niche = "medicine"
    elif any(k in combined_text for k in ("инвестиц", "лизинг", "финанс", "кредит", "страхован")):
        regulated_niche = "finance"

    # 2. Целевая ниша, запросы и роли ЛПР
    target_niche = niche_hint.strip() or "Компании малого и среднего бизнеса РФ"
    niche_l = target_niche.lower()

    if any(k in niche_l for k in ("адвокат", "юрист", "юридич", "право")):
        industries = ["Адвокатские бюро", "Коллегии адвокатов", "Юридические фирмы"]
        queries = [
            "адвокатское бюро",
            "коллегия адвокатов",
            "адвокат по арбитражным делам",
            "адвокат по уголовным делам",
            "юридическая компания сопровождение бизнеса",
        ]
        titles = [
            "управляющий партнёр",
            "старший партнёр",
            "председатель коллегии",
            "руководитель практики",
            "адвокат",
        ]
    elif any(k in niche_l for k in ("стоматолог", "клиник", "медицин", "косметолог")):
        industries = ["Частные клиники", "Стоматология", "Медицинские центры"]
        queries = [
            "стоматологическая клиника",
            "многопрофильный медицинский центр",
            "клиника эстетической медицины",
            "центр имплантации",
        ]
        titles = ["главный врач", "управляющий клиникой", "собственник", "директор по маркетингу"]
    elif any(k in niche_l for k in ("завод", "производств", "металл", "оборудован", "опт")):
        industries = ["Производство", "Оптовые поставки", "Промышленность"]
        queries = [
            target_niche.lower(),
            "производственная компания",
            "оптовый поставщик оборудования",
            "торгово-производственная компания",
        ]
        titles = ["генеральный директор", "коммерческий директор", "директор по развитию", "собственник"]
    elif any(k in niche_l for k in ("ресторан", "кафе", "кофейн", "пекарн", "horeca", "отел")):
        industries = ["Рестораны и кафе", "Сети кофеен", "Гостиничный бизнес"]
        queries = ["сеть кофеен", "ресторан", "ремесленная пекарня", "бутик-отель"]
        titles = ["управляющий", "владелец", "директор по закупкам", "операционный директор"]
    elif any(k in niche_l for k in ("строй", "девелоп", "ремонт", "ижс", "архитект")):
        industries = ["Строительство и ИЖС", "Проектирование", "Ремонт коммерческих помещений"]
        queries = [
            "строительство загородных домов",
            "архитектурное бюро",
            "генподрядная организация",
            "ремонт коммерческих помещений",
        ]
        titles = ["генеральный директор", "коммерческий директор", "главный инженер", "основатель"]
    else:
        # Универсальная генерация запросов из формулировки ниши пользователя
        raw_parts = [p.strip() for p in re.split(r"[,;/]|(?:\s+и\s+)", target_niche) if len(p.strip()) >= 3]
        queries = list(dict.fromkeys([*(p.lower() for p in raw_parts), target_niche.lower()]))[:5]
        industries = [p.capitalize() for p in raw_parts[:3]] or [target_niche]
        titles = [
            "генеральный директор",
            "коммерческий директор",
            "директор по маркетингу",
            "основатель",
            "собственник",
        ]

    # 3. Определяем продукт, категорию, мягкий оффер и сигнальные вакансии hh.ru по брифу
    brief_clean = brief.strip() or site_meta.strip() or f"Профессиональные решения для сегмента «{target_niche}»"
    brief_l = brief_clean.lower()

    if any(k in brief_l for k in ("сайт", "seo", "сео", "продвижен", "трафик", "директ", "маркетинг")):
        prod_category = "Разработка сайтов и поисковое продвижение (SEO)"
        first_clause = re.split(r"[.!?]", brief_clean)[0].strip()
        prod_name = first_clause[:90] if 10 <= len(first_clause) <= 95 else f"Разработка сайтов и SEO для ниши «{target_niche}»"
        entry_offer = "прислать короткий разбор из 3 конкретных пунктов по структуре сайта и поисковой видимости"
        hiring_signals = ["маркетолог", "контент-менеджер", "помощник руководителя"]
        benefits = [
            "Проектирование структуры страниц под реальный поисковый спрос в Яндексе",
            "Последовательный план работ с учётом приоритетных направлений и бюджета",
            "Учёт отраслевых и репутационных ограничений ниши клиента",
        ]
    elif any(k in brief_l for k in ("crm", "црм", "битрикс", "amocrm", "аналитик", "автоматизац")):
        prod_category = "Интеграция CRM и сквозной аналитики"
        prod_name = f"Внедрение CRM и автоматизация обработки заявок ({company_name})"
        entry_offer = "прислать короткий разбор пути клиента и точек потери обращений на сайте"
        hiring_signals = ["менеджер по продажам", "руководитель отдела продаж"]
        benefits = [
            "Фиксация всех звонков, мессенджеров и заявок с сайта в едином окне",
            "Прозрачная аналитика конверсии без ручных таблиц",
        ]
    elif any(k in brief_l for k in ("логист", "тамож", "вэд", "доставк", "перевоз")):
        prod_category = "Логистика и сопровождение поставок"
        prod_name = f"Логистика и сопровождение поставок ({company_name})"
        entry_offer = "рассчитать сроки и ставку на вашем типовом маршруте"
        hiring_signals = ["менеджер по закупкам", "менеджер ВЭД", "логист"]
        benefits = [
            "Прозрачный расчёт сроков и полный комплект закрывающих документов",
            "Контроль груза на каждом участке маршрута",
        ]
    else:
        first_clause = re.split(r"[.!?]", brief_clean)[0].strip()
        prod_category = "B2B-услуги и решения"
        prod_name = first_clause[:90] if 8 <= len(first_clause) <= 95 else f"Решения {company_name} для бизнеса"
        entry_offer = "прислать короткий расчёт и 3 конкретных предложения под ваши задачи"
        hiring_signals = ["менеджер по продажам", "руководитель направления"]
        benefits = [
            "Фокус на измеримом результате и прозрачном плане работ",
            "Адаптация под специфику и бюджет компании клиента",
        ]

    target_cities = [c.strip() for c in (cities or []) if c.strip()] or ["Москва", "Санкт-Петербург"]

    competitive_md = (
        f"# Позиционирование и отстройка: {company_name}\n\n"
        f"## Целевой сегмент\n"
        f"- **Ниша**: {target_niche}\n"
        f"- **География**: {', '.join(target_cities)}\n\n"
        f"## Чем наше предложение отличается от типовых подрядчиков\n"
        f"- Глубокое погружение в специфику сегмента «{target_niche}» вместо универсальных потоковых шаблонов.\n"
        f"- Начинаем с предметного разбора текущей ситуации (точки потери обращений, структура спроса, приоритеты бюджета).\n"
        f"- Спокойная деловая коммуникация с опорой на проверяемые факты.\n"
    )

    return {
        "persona": {
            "company": company_name,
            "website": clean_url,
            "role": "Руководитель проектов",
        },
        "product": {
            "name": prod_name,
            "category": prod_category,
            "description": brief_clean,
            "key_benefits": benefits,
            "offer": {
                "entry": entry_offer,
                "goal": "send_audit",
            },
        },
        "icp": {
            "niche": target_niche,
            "industries": industries,
            "search_queries": queries,
            "cities": target_cities,
            "decision_maker_titles": titles,
            "hiring_signals": hiring_signals,
        },
        "compliance": {
            "regulated_niche": regulated_niche,
        },
        "competitive_intel_md": competitive_md,
    }


def render_default_product_skill(cfg: FridayConfig) -> str:
    """Генерирует структурированный `skills/product_knowledge.md` по настройкам ниши."""
    queries = "\n".join(f"- `{q}`" for q in cfg.icp.search_queries) or "- (не заданы)"
    benefits = "\n".join(f"- {b}" for b in cfg.product.key_benefits) or "- Измеримая польза для бизнеса клиента"
    titles = ", ".join(cfg.icp.decision_maker_titles) or "руководитель, основатель"
    hiring = ", ".join(cfg.icp.hiring_signals) or "—"
    cities = ", ".join(cfg.icp.cities) or "Россия"
    return (
        f"# Профиль продукта и ниши: {cfg.persona.company}\n\n"
        f"- **Продукт**: {cfg.product.name}\n"
        f"- **Сайт**: {cfg.persona.website or 'не указан'}\n"
        f"- **Целевая аудитория (ICP)**: {cfg.icp.niche}\n"
        f"- **Города**: {cities}\n"
        f"- **Ключевые ЛПР**: {titles}\n"
        f"- **Сигнальные вакансии hh.ru**: {hiring}\n\n"
        f"## Суть предложения\n\n"
        f"{cfg.product.description}\n\n"
        f"## Ключевые преимущества\n\n"
        f"{benefits}\n\n"
        f"## Первый шаг (мягкий оффер)\n\n"
        f"{cfg.product.offer.entry}\n\n"
        f"## Поисковые запросы\n\n"
        f"{queries}\n"
    )


def apply_niche_setup(
    data: dict,
    *,
    base_config: FridayConfig | None = None,
    local_config_path: Path | None = None,
    skills_dir: Path | None = None,
) -> dict:
    """Применяет конфигурацию ниши и продукта.

    Записывает `friday.local.yaml`, `skills/product_knowledge.md` и `skills/competitive_intel.md`.
    """
    cfg = base_config or load_config()
    persona = data.get("persona") or {}
    product = data.get("product") or {}
    icp = data.get("icp") or {}
    comp = data.get("compliance") or {}

    if persona.get("name"):
        cfg.persona.name = str(persona["name"]).strip()
    if persona.get("company"):
        cfg.persona.company = str(persona["company"]).strip()
    if persona.get("role"):
        cfg.persona.role = str(persona["role"]).strip()
    if "telegram" in persona and persona["telegram"] is not None:
        cfg.persona.telegram = str(persona["telegram"]).strip()
    if "website" in persona and persona["website"] is not None:
        cfg.persona.website = str(persona["website"]).strip()
    if persona.get("tone"):
        cfg.persona.tone = str(persona["tone"]).strip()
    if persona.get("address_style") in ("polite_name", "formal_vy"):
        cfg.persona.address_style = persona["address_style"]

    if product.get("name"):
        cfg.product.name = str(product["name"]).strip()
    if "category" in product and product["category"] is not None:
        cfg.product.category = str(product["category"]).strip()
    if product.get("description"):
        cfg.product.description = str(product["description"]).strip()
    if "pricing" in product and product["pricing"] is not None:
        cfg.product.pricing = str(product["pricing"]).strip()
    if isinstance(product.get("key_benefits"), list):
        cfg.product.key_benefits = [str(x).strip() for x in product["key_benefits"] if str(x).strip()]
    offer = product.get("offer") or {}
    if isinstance(offer, dict):
        if "entry" in offer and offer["entry"] is not None:
            cfg.product.offer.entry = str(offer["entry"]).strip()
        if offer.get("goal") in ("get_reply", "book_call", "send_audit"):
            cfg.product.offer.goal = offer["goal"]

    if icp.get("niche"):
        cfg.icp.niche = str(icp["niche"]).strip()
    if isinstance(icp.get("industries"), list):
        cfg.icp.industries = [str(x).strip() for x in icp["industries"] if str(x).strip()]
    if isinstance(icp.get("cities"), list):
        cfg.icp.cities = [str(x).strip() for x in icp["cities"] if str(x).strip()]
    if isinstance(icp.get("search_queries"), list):
        cfg.icp.search_queries = [str(x).strip() for x in icp["search_queries"] if str(x).strip()]
    if isinstance(icp.get("decision_maker_titles"), list):
        cfg.icp.decision_maker_titles = [
            str(x).strip() for x in icp["decision_maker_titles"] if str(x).strip()
        ]
    if isinstance(icp.get("hiring_signals"), list):
        cfg.icp.hiring_signals = [str(x).strip() for x in icp["hiring_signals"] if str(x).strip()]
    if icp.get("company_size"):
        cfg.icp.company_size = str(icp["company_size"]).strip()
    if "min_fit_score" in icp and icp["min_fit_score"] is not None:
        cfg.icp.min_fit_score = max(0, min(100, int(icp["min_fit_score"])))

    reg = str(comp.get("regulated_niche") or data.get("regulated_niche") or "").strip()
    if reg in ("none", "medicine", "finance", "legal_advocates"):
        cfg.compliance.regulated_niche = reg

    saved_cfg_path = save_local_config(cfg, local_config_path or LOCAL_CONFIG_PATH)

    target_skills_dir = skills_dir or SKILLS_DIR
    target_skills_dir.mkdir(parents=True, exist_ok=True)

    product_md = str(data.get("product_knowledge_md") or "").strip()
    if not product_md:
        product_md = render_default_product_skill(cfg)
    prod_path = target_skills_dir / "product_knowledge.md"
    prod_path.write_text(product_md + "\n", encoding="utf-8")

    comp_md = str(data.get("competitive_intel_md") or "").strip()
    comp_path = target_skills_dir / "competitive_intel.md"
    if comp_md:
        comp_path.write_text(comp_md + "\n", encoding="utf-8")

    return {
        "config_path": str(saved_cfg_path),
        "product_skill_path": str(prod_path),
        "competitive_skill_path": str(comp_path) if comp_md else "",
        "company_name": cfg.persona.company,
        "product_name": cfg.product.name,
        "niche": cfg.icp.niche,
        "regulated_niche": cfg.compliance.regulated_niche,
    }


def build_train_prompt_context(
    *,
    website_crawl: dict,
    user_brief: str = "",
    target_niche_hint: str = "",
    cities_hint: list[str] | None = None,
) -> str:
    """Формирует защищённый `<untrusted>` контекст для задачи `train_niche`."""
    blocks = [UNTRUSTED_RULE]
    if user_brief.strip():
        blocks.append(f"Описание от пользователя:\n{user_brief.strip()}")
    if target_niche_hint.strip():
        blocks.append(f"Желаемая целевая ниша (ICP): {target_niche_hint.strip()}")
    if cities_hint:
        blocks.append(f"Целевые города РФ: {', '.join(cities_hint)}")

    pages = website_crawl.get("pages") or []
    if pages:
        page_lines = []
        for p in pages:
            page_lines.append(
                f"URL: {p.get('url', '')}\n"
                f"Title: {p.get('title', '')}\n"
                f"Description: {p.get('meta_description', '')}\n"
                f"Headings: {' | '.join(p.get('headings') or [])}\n"
                f"Excerpt: {p.get('excerpt', '')}"
            )
        blocks.append(
            "Материалы с сайта продукта пользователя (внешний контент):\n"
            + wrap_untrusted("\n\n---\n\n".join(page_lines), source="user_product_website")
        )
    elif website_crawl.get("error"):
        blocks.append(f"Примечание: сайт не удалось скачать автоматически ({website_crawl['error']}). Опирайся на бриф пользователя.")

    return "\n\n".join(blocks)
