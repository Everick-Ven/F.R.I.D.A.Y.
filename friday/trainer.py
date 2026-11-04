"""Шаг 1: обучение F.R.I.D.A.Y. продукту пользователя и целевой нише РФ.

Поддерживает два сценария настройки:
1. Интеллектуальный разбор через ИИ-агента рабочей среды (`friday train <url_или_описание>`):
   Python скачивает главную и ключевые страницы сайта пользователя ($0, SSRF-safe),
   оборачивает текст в `<untrusted>` и ставит задачу `train_niche` для ИИ-агента.
   Агент формирует `friday.local.yaml`, `skills/product_knowledge.md` и `skills/competitive_intel.md`.
2. Прямое сохранение анкеты ниши и продукта (через веб-интерфейс или CLI),
   которое сразу записывает `friday.local.yaml` и `skills/product_knowledge.md`.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

from friday.config import FridayConfig, load_config, save_local_config
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

# Примеры шаблонов для быстрой подстановки в мастере настройки (база НЕ предзаполнена ими)
NICHE_TEMPLATES_RU: list[dict] = [
    {
        "id": "b2b_crm_integrator",
        "label": "B2B IT / Интеграция CRM и сквозной аналитики",
        "regulated_niche": "none",
        "persona": {
            "name": "Алексей Смирнов",
            "company": "КонтурПоток Интеграция",
            "role": "Руководитель проектов внедрения",
            "telegram": "@konturpotok_crm",
            "website": "https://example-crm.ru",
        },
        "product": {
            "name": "Внедрение amoCRM / Битрикс24 и сквозной аналитики за 14 дней",
            "category": "B2B IT / Автоматизация продаж",
            "description": (
                "Настраиваем связку сайта, телефонии, мессенджеров и CRM, "
                "чтобы заявки с Яндекс.Директа и карт не терялись между менеджерами."
            ),
            "pricing": "от 65 000 ₽ за проект под ключ",
            "key_benefits": [
                "Автоматическая фиксация всех звонков, форм и чатов Telegram/WhatsApp в CRM",
                "Прозрачная воронка для РОПа и собственника без таблиц Excel",
            ],
            "offer": {
                "entry": "прислать короткий 5-минутный разбор пути заявки на вашем сайте из 3 пунктов без созвона",
                "goal": "send_audit",
            },
        },
        "icp": {
            "niche": "Оптовые, производственные и сервисные компании РФ с отделом продаж",
            "industries": ["Производство", "Оптовая торговля", "Строительные услуги"],
            "cities": ["Москва", "Санкт-Петербург", "Екатеринбург", "Казань", "Новосибирск"],
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
        "label": "Маркетинговое / SEO и геомаркетинговое агентство",
        "regulated_niche": "none",
        "persona": {
            "name": "Мария Власова",
            "company": "ГеоРост Digital",
            "role": "Руководитель поискового направления",
            "telegram": "@georost_seo",
            "website": "https://example-agency.ru",
        },
        "product": {
            "name": "Продвижение в Яндекс Поиске и геосервисах (2ГИС, Яндекс Карты)",
            "category": "Поисковый и локальный маркетинг",
            "description": (
                "Выводим локальный бизнес и сети услуг на первую страницу Яндекса и в топ карточек 2ГИС / Яндекс Карт "
                "за счёт отдельных посадочных страниц под услуги и работы с репутацией."
            ),
            "pricing": "от 55 000 ₽/мес",
            "key_benefits": [
                "Рост органических обращений из Яндекса и карт без перегрева ставок в Директе",
                "Проработка структуры посадочных под коммерческий спрос",
            ],
            "offer": {
                "entry": "прислать список из 3 точек потери органического трафика по вашему домену и карточке",
                "goal": "send_audit",
            },
        },
        "icp": {
            "niche": "Локальные сети услуг, стоматологии, автосервисы и частные центры",
            "industries": ["Локальные услуги", "Автобизнес", "Частные клиники"],
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
        "label": "Логистика, фулфилмент и ВЭД",
        "regulated_niche": "none",
        "persona": {
            "name": "Дмитрий Орлов",
            "company": "ВостокТранс ВЭД",
            "role": "Директор по работе с корпоративными клиентами",
            "telegram": "@vostoktrans_ved",
            "website": "https://example-ved.ru",
        },
        "product": {
            "name": "Международная логистика, белая доставка и таможенное оформление",
            "category": "Логистика и ВЭД",
            "description": (
                "Организуем прямые поставки комплектующих и оборудования с прозрачными валютными платежами "
                "и полным комплектом закрывающих документов и ГТД."
            ),
            "pricing": "расчёт ставки под маршрут за 2 часа",
            "key_benefits": [
                "Фиксированные сроки прохождения границы и белое таможенное оформление",
                "Отсрочка и страхование груза на всём плече",
            ],
            "offer": {
                "entry": "сделать расчёт альтернативного маршрута и ставки на одной вашей типовой партии",
                "goal": "get_reply",
            },
        },
        "icp": {
            "niche": "Торговые дома, дистрибьюторы и производители с импортом комплектующих",
            "industries": ["Дистрибуция", "Импорт оборудования", "Электроника и комплектующие"],
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
        "label": "Поставки и B2B-сервис для HoReCa и ритейла",
        "regulated_niche": "none",
        "persona": {
            "name": "Екатерина Морозова",
            "company": "ПрофХорека Сервис",
            "role": "Руководитель отдела HoReCa",
            "telegram": "@profhoreca_b2b",
            "website": "https://example-horeca.ru",
        },
        "product": {
            "name": "Поставка ингредиентов / кофейного оборудования и сервис 24/7 для заведений",
            "category": "Поставки для HoReCa",
            "description": (
                "Бесперебойные поставки для кофеен, пекарен и ресторанов с отсрочкой платежа "
                "и выездом инженера с подменным оборудованием в день обращения."
            ),
            "pricing": "оптовый прайс от 15 000 ₽ заказа",
            "key_benefits": [
                "Подменное оборудование в день заявки без простоя точки",
                "Стабильная обжарка и матрица позиций под сетевые стандарты",
            ],
            "offer": {
                "entry": "отправить актуальный оптовый прайс и привезти бесплатный дегустационный сет",
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


async def crawl_product_site(website_url: str) -> dict:
    """Безопасно скачивает главную и до 2 ключевых подстраниц сайта продукта пользователя."""
    url = website_url.strip()
    if not url:
        return {"url": "", "pages": [], "error": ""}
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    pages: list[dict] = []
    client = SafePublicHttpClient(timeout=12.0)
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


def render_default_product_skill(cfg: FridayConfig) -> str:
    """Генерирует структурированный `skills/product_knowledge.md` по настройкам ниши."""
    queries = "\n".join(f"- `{q}`" for q in cfg.icp.search_queries) or "- (не заданы)"
    benefits = "\n".join(f"- {b}" for b in cfg.product.key_benefits) or "- Конкретная измеримая польза для клиента"
    titles = ", ".join(cfg.icp.decision_maker_titles) or "руководитель, основатель"
    hiring = ", ".join(cfg.icp.hiring_signals) or "нет специальных вакансий"
    cities = ", ".join(cfg.icp.cities) or "Россия"
    return (
        f"# Знание продукта и целевой ниши: {cfg.persona.company}\n\n"
        f"- **Продукт / услуга**: {cfg.product.name} ({cfg.product.category})\n"
        f"- **Сайт**: {cfg.persona.website or 'не указан'}\n"
        f"- **Целевая ниша (ICP)**: {cfg.icp.niche}\n"
        f"- **География**: {cities}\n"
        f"- **Ключевые роли ЛПР**: {titles}\n"
        f"- **Сигнальные вакансии на hh.ru**: {hiring}\n"
        f"- **Ценовая модель**: {cfg.product.pricing or 'по запросу'}\n\n"
        f"## Описание продукта\n\n"
        f"{cfg.product.description}\n\n"
        f"## Ключевые выгоды\n\n"
        f"{benefits}\n\n"
        f"## Мягкий первый шаг (Entry Offer)\n\n"
        f"{cfg.product.offer.entry}\n\n"
        f"## Поисковые запросы для поиска компаний\n\n"
        f"{queries}\n"
    )


def apply_niche_setup(
    data: dict,
    *,
    base_config: FridayConfig | None = None,
    local_config_path: Path | None = None,
    skills_dir: Path | None = None,
) -> dict:
    """Применяет конфигурацию ниши и продукта (из ответа ИИ-агента или формы UI).

    Поддерживает схему `persona` + `product` + `icp` + `compliance` (из `friday.yaml` и `prompts/train.md`).
    Записывает:
    - `friday.local.yaml`
    - `skills/product_knowledge.md`
    - `skills/competitive_intel.md` (если передан)
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
    """Формирует защищённый `<untrusted>` контекст и точную JSON-схему для задачи `train_niche`."""
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

    blocks.append(
        "Обязательная структура JSON-ответа:\n"
        "{\n"
        '  "persona": {"name": "...", "company": "...", "role": "...", "website": "..."},\n'
        '  "product": {\n'
        '    "name": "...", "category": "...", "description": "...", "pricing": "...",\n'
        '    "key_benefits": ["...", "..."],\n'
        '    "offer": {"entry": "...", "goal": "send_audit"}\n'
        "  },\n"
        '  "icp": {\n'
        '    "niche": "...",\n'
        '    "industries": ["...", "..."],\n'
        '    "search_queries": ["...", "..."],\n'
        '    "cities": ["Москва", "Санкт-Петербург"],\n'
        '    "company_size": "10–200 сотрудников",\n'
        '    "decision_maker_titles": ["...", "..."],\n'
        '    "hiring_signals": ["...", "..."]\n'
        "  },\n"
        '  "compliance": {"regulated_niche": "none"},\n'
        '  "product_knowledge_md": "# ...",\n'
        '  "competitive_intel_md": "# ..."\n'
        "}"
    )
    return "\n\n".join(blocks)
