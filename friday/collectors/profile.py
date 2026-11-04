"""PROFILE — профилирование российских сайтов адвокатов, бюро и коллегий ($0, без LLM).

Проверяет за 2–4 лёгких HTTP-запроса (главная + sitemap.xml + страница команды/контактов):
- наличие счётчика Яндекс.Метрики (`NO_YANDEX_METRIKA`);
- признаки платного трафика и коллтрекинга: Яндекс.Директ / РСЯ, Calltouch, Roistat, CoMagic, Marquiz (`RUNNING_YANDEX_DIRECT`);
- российский стек сайта: Tilda, 1С-Битрикс, WordPress, Битрикс24, amoCRM, Jivo, Envybox (`TECH_STACK_RU`);
- наличие отдельных посадочных страниц под специализации (`MISSING_PRACTICE_PAGES`);
- прозрачность для доверителя: реестровые номера Минюста, форма образования, раздел практики (`WEAK_TRUST_TRANSPARENCY`);
- риски 38-ФЗ «О рекламе» и ст. 17 КПЭА прямо на сайте практики (`COMPLIANCE_RISK_ON_SITE`);
- кнопки быстрого перехода в Telegram / WhatsApp (`NO_FAST_MESSENGER_CTA`, `TELEGRAM_CONTACT`);
- публичный рабочий email (`PUBLIC_EMAIL_CONTACT`), форму контактов (`CONTACT_FORM_URL`),
  Schema.org (`NO_SCHEMA_MARKUP`), SSL/viewport (`NO_SSL_OR_MOBILE_ISSUES`), `sitemap.xml` (`SITE_PAGE_COUNT`, `BLOG_STALE`)
  и подрядчика в подвале (`INCUMBENT_AGENCY`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from friday.collectors.discover import REGISTRY_NUMBER_RE, detect_legal_form
from friday.collectors.people import extract_people_from_html
from friday.config import FridayConfig
from friday.gate import display_domain, normalize_domain_idna
from friday.models import Company, Prospect
from friday.public_http import UnsafeURLError, public_http_client
from friday.state import StateManager
from friday.textio import decode_russian_bytes

logger = logging.getLogger("friday.collectors.profile")

TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=8.0)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Российский технологический стек и виджеты
TECH_FINGERPRINTS_RU: dict[str, tuple[str, ...]] = {
    "Tilda": (r"tildacdn\.com", r"tilda-blocks", r"data-tilda-project-id"),
    "1С-Битрикс": (r"/bitrix/js/", r"/bitrix/templates/", r"BX\.setJSList"),
    "WordPress": (r"/wp-content/", r"/wp-includes/"),
    "Яндекс.Метрика": (r"mc\.yandex\.ru/metrika", r"\bym\s*\(\s*\d{5,}"),
    "Битрикс24": (r"cdn(?:-ru)?\.bitrix24\.ru", r"b24-widget"),
    "amoCRM": (r"amocrm\.ru", r"amoforms"),
    "Jivo": (r"code\.jivosite\.com", r"jivo\.ru"),
    "Calltouch": (r"mod\.calltouch\.ru", r"calltouch"),
    "Roistat": (r"cloud\.roistat\.com", r"roistat_visit"),
    "CoMagic / UIS": (r"app\.comagic\.ru", r"uiscom\.ru"),
    "Mango Office": (r"widgets\.mango-office\.ru",),
    "Marquiz": (r"script\.marquiz\.ru", r"marquiz\.ru"),
    "Envybox": (r"cbk\.ws", r"envybox\.io"),
    "YCLIENTS": (r"yclients\.com",),
}

YANDEX_METRIKA_PATTERNS = (
    re.compile(r"mc\.yandex\.ru/metrika", re.I),
    re.compile(r"\bym\s*\(\s*\d{5,}\s*,", re.I),
    re.compile(r"yaCounter\d{5,}", re.I),
)

PAID_TRAFFIC_SIGNALS_RU: dict[str, re.Pattern] = {
    "Яндекс.Директ / РСЯ": re.compile(r"yandex\.ru/ads|yaContextCb|yclid", re.I),
    "Calltouch": re.compile(r"calltouch\.ru", re.I),
    "Roistat": re.compile(r"roistat\.com", re.I),
    "CoMagic / UIS": re.compile(r"comagic\.ru|uiscom\.ru", re.I),
    "Marquiz": re.compile(r"marquiz\.ru", re.I),
    "Yagla": re.compile(r"yagla\.ru", re.I),
}

# Ключевые слоги и слова узких юридических специализаций (для проверки отдельных посадочных страниц)
PRACTICE_URL_OR_TEXT_RE = re.compile(
    r"(?:ugolov|semeyn|arbitrazh|bankrot|nasled|zhilishch|zemel|nalog|korporat|dtp|trudov|"
    r"уголовн|семейн|арбитраж|банкротств|наследств|жилищн|земельн|налогов|корпоративн|трудов|защита\s+бизнеса)",
    re.I,
)

# Признаки рискованных формулировок по 38-ФЗ и ст. 17 КПЭА прямо на сайте практики
SITE_COMPLIANCE_RISKS_RU: tuple[tuple[re.Pattern, str], ...] = (
    (
        re.compile(r"100\s*%\s*(?:гарант\w*|выигрыш\w*|успех\w*|результат\w*)", re.I),
        "обещание «100% гарантии/результата» (риск ст. 5 ФЗ-38 и ст. 17 КПЭА)",
    ),
    (
        re.compile(r"гарантир\w+\s+(?:выигрыш|положительн\w+\s+исход|оправдательн\w+|списание\s+долг)", re.I),
        "обещание гарантированного исхода дела (риск ст. 17 КПЭА)",
    ),
    (
        re.compile(r"\bлучш(?:ий|ие|ая)\s+адвокат\w*\b", re.I),
        "оценочная характеристика «лучший адвокат» (риск ст. 17 КПЭА и ст. 5 ФЗ-38)",
    ),
    (
        re.compile(r"отзывы\s+(?:наших\s+)?доверителей", re.I),
        "раздел отзывов доверителей на сайте адвокатского образования (риск ст. 17 п. 2 КПЭА)",
    ),
)

FOOTER_AGENCY_RE = re.compile(
    r"(?:разработк\w*\s+(?:и\s+продвижени\w*\s+)?сайта|создание\s+сайта|сделано\s+в|продвижение\s+сайта)"
    r"\s*[:—–\-]?\s*([A-Za-zА-Яа-яЁё0-9 ._-]{2,40})",
    re.I,
)
BENIGN_CREDITS = ("tilda", "тильда", "битрикс", "bitrix", "wordpress", "конструктор")

TELEGRAM_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,64})", re.I)
WHATSAPP_LINK_RE = re.compile(r"(?:https?://)?(?:wa\.me|api\.whatsapp\.com/send\?phone=)([+\d]{8,16})", re.I)
TENCHAT_LINK_RE = re.compile(r"https?://(?:www\.)?tenchat\.ru/[A-Za-z0-9_.-]+", re.I)
VK_LINK_RE = re.compile(r"https?://(?:www\.)?vk\.com/[A-Za-z0-9_.-]+", re.I)
MAILTO_RE = re.compile(r"mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-zА-Яа-яЁё]{2,})", re.I)
TEXT_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(?:ru|su|com|org|net|рф))\b", re.I)
RU_PHONE_RE = re.compile(r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")

TEAM_LINK_WORDS = ("команда", "адвокаты", "партнеры", "партнёры", "о коллегии", "о бюро", "о нас", "специалисты", "состав")
CONTACT_LINK_WORDS = ("контакты", "связаться", "обсудить", "обратная связь", "консультация")


def analyze_practice_site_html(
    html: str,
    *,
    base_url: str,
    sitemap_xml: str = "",
    extra_pages_html: list[tuple[str, str]] | None = None,
) -> dict:
    """Чистая функция анализа HTML российского юридического сайта (удобна для модульных тестов и офлайн-разбора)."""
    soup = BeautifulSoup(html or "", "html.parser")
    all_html_chunks = [(base_url, html or ""), *(extra_pages_html or [])]
    combined_html = "\n".join(chunk for _, chunk in all_html_chunks)
    plain_text = unescape(re.sub(r"\s+", " ", soup.get_text(" ", strip=True)))
    combined_plain = unescape(
        re.sub(r"\s+", " ", BeautifulSoup(combined_html, "html.parser").get_text(" ", strip=True))
    )

    # 1. Название и описание
    og_site = soup.find("meta", attrs={"property": "og:site_name"})
    title_tag = soup.find("title")
    desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    raw_title = (
        (og_site.get("content") if og_site else "")
        or (title_tag.get_text(" ", strip=True) if title_tag else "")
    ).strip()
    description = (desc_tag.get("content", "").strip() if desc_tag else "")[:300]

    # 2. Технологический стек и аналитика
    tech_stack: list[str] = []
    for name, regexes in TECH_FINGERPRINTS_RU.items():
        if any(re.search(rx, combined_html, re.I) for rx in regexes):
            tech_stack.append(name)

    has_metrika = any(p.search(combined_html) for p in YANDEX_METRIKA_PATTERNS)
    paid_tools = [label for label, pat in PAID_TRAFFIC_SIGNALS_RU.items() if pat.search(combined_html)]

    # 3. Проверка посадочных страниц под практики
    practice_links: set[str] = set()
    contact_form_url = ""
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        anchor = a_tag.get_text(" ", strip=True).lower()
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        if normalize_domain_idna(parsed.netloc) != normalize_domain_idna(base_url):
            continue
        path = parsed.path.rstrip("/")
        if path and path != "" and len(path) > 2:
            if PRACTICE_URL_OR_TEXT_RE.search(path) or PRACTICE_URL_OR_TEXT_RE.search(anchor):
                practice_links.add(path.lower())
        if not contact_form_url and any(w in anchor for w in CONTACT_LINK_WORDS):
            contact_form_url = full_url

    if not contact_form_url and soup.find("form"):
        contact_form_url = base_url

    # 4. Проверка прозрачности (реестровые номера, форма образования, раздел практики/дел)
    has_registry_no = bool(REGISTRY_NUMBER_RE.search(combined_plain))
    legal_form = detect_legal_form(raw_title + " " + combined_plain[:2500])
    has_cases_section = bool(
        re.search(r"\b(?:судебная\s+практика|наша\s+практика|завершенные\s+дела|кейсы|дела\s+коллегии)\b", combined_plain, re.I)
    )
    weak_transparency = not (has_registry_no or (legal_form in ("Коллегия адвокатов", "Адвокатское бюро", "Адвокатский кабинет") and has_cases_section))

    # 5. Риски 38-ФЗ и ст. 17 КПЭА на самом сайте практики
    is_advocate_site = "адвокат" in combined_plain.lower()
    compliance_risks: list[str] = []
    for pat, label in SITE_COMPLIANCE_RISKS_RU:
        if "КПЭА" in label and "отзывы" in label and not is_advocate_site:
            continue
        if pat.search(combined_plain):
            compliance_risks.append(label)

    # 6. Контакты: Telegram, WhatsApp, Email, Телефон, TenChat, VK
    tg_matches = [
        m.group(1) for m in TELEGRAM_LINK_RE.finditer(combined_html)
        if m.group(1).lower() not in ("share", "joinchat", "iv")
    ]
    telegram = f"@{tg_matches[0]}" if tg_matches else ""
    wa_match = WHATSAPP_LINK_RE.search(combined_html)
    whatsapp = wa_match.group(0) if wa_match else ""
    tc_match = TENCHAT_LINK_RE.search(combined_html)
    tenchat = tc_match.group(0) if tc_match else ""
    vk_match = VK_LINK_RE.search(combined_html)
    vk = vk_match.group(0) if vk_match else ""

    emails = [m.group(1).lower() for m in MAILTO_RE.finditer(combined_html)]
    if not emails:
        emails = [
            m.group(1).lower() for m in TEXT_EMAIL_RE.finditer(combined_plain)
            if not m.group(1).lower().endswith((".png", ".jpg", ".webp"))
        ]
    public_email = emails[0] if emails else ""

    ph_match = RU_PHONE_RE.search(combined_plain)
    phone = ph_match.group(0).strip() if ph_match else ""

    has_fast_messenger = bool(telegram or whatsapp)
    has_schema = 'application/ld+json' in combined_html.lower() or 'itemtype="http://schema.org' in combined_html.lower() or 'itemtype="https://schema.org' in combined_html.lower()
    has_viewport = bool(soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}))
    is_https = base_url.lower().startswith("https://")

    # 7. Подрядчик в подвале
    incumbent_agency = ""
    footer_el = soup.find("footer")
    footer_text = footer_el.get_text(" ", strip=True) if footer_el else plain_text[-600:]
    ag_m = FOOTER_AGENCY_RE.search(footer_text)
    if ag_m:
        cand = ag_m.group(1).strip(" .,|-")
        if cand and not any(b in cand.lower() for b in BENIGN_CREDITS):
            incumbent_agency = cand

    # 8. Карта сайта (sitemap.xml)
    page_count: int | None = None
    stale_days: int | None = None
    if sitemap_xml and "<loc>" in sitemap_xml:
        locs = re.findall(r"<loc>(.*?)</loc>", sitemap_xml, re.I)
        page_count = len(locs)
        for loc in locs:
            if PRACTICE_URL_OR_TEXT_RE.search(loc):
                practice_links.add(urlparse(loc).path.lower())
        lastmods = re.findall(r"<lastmod>\s*(\d{4}-\d{2}-\d{2})", sitemap_xml, re.I)
        if lastmods:
            try:
                newest = max(datetime.strptime(d, "%Y-%m-%d").date() for d in lastmods)
                stale_days = max(0, (date.today() - newest).days)
            except ValueError:
                pass

    missing_practice_pages = len(practice_links) < 2

    # 9. ЛПР и адвокаты со всех прочитанных страниц
    people: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for url, page_h in all_html_chunks:
        for person in extract_people_from_html(page_h, page_url=url):
            key = (person["last_name"].lower(), person["first_name"].lower())
            if key not in seen_keys:
                seen_keys.add(key)
                people.append(person)
    people.sort(key=lambda x: (x["rank"], bool(x["middle_name"])), reverse=True)

    return {
        "site_title": raw_title[:120],
        "legal_form": legal_form,
        "description": description,
        "tech_stack": tech_stack,
        "has_metrika": has_metrika,
        "paid_tools": paid_tools,
        "missing_practice_pages": missing_practice_pages,
        "practice_pages_found": len(practice_links),
        "weak_transparency": weak_transparency,
        "compliance_risks": compliance_risks,
        "has_fast_messenger": has_fast_messenger,
        "has_schema": has_schema,
        "ssl_or_mobile_issues": (not is_https) or (not has_viewport),
        "page_count": page_count,
        "stale_days": stale_days,
        "incumbent_agency": incumbent_agency,
        "telegram": telegram,
        "whatsapp": whatsapp,
        "tenchat": tenchat,
        "vk": vk,
        "public_email": public_email,
        "phone": phone,
        "contact_form_url": contact_form_url,
        "people": people,
    }


def _find_internal_subpages(html: str, base_url: str) -> list[str]:
    """Найти до 2 внутренних страниц («Команда / Адвокаты» и «Контакты») из меню сайта."""
    soup = BeautifulSoup(html or "", "html.parser")
    base_dom = normalize_domain_idna(base_url)
    team_urls: list[str] = []
    contact_urls: list[str] = []

    for a_tag in soup.find_all("a", href=True):
        anchor = a_tag.get_text(" ", strip=True).lower()
        full = urljoin(base_url, a_tag["href"].strip())
        if normalize_domain_idna(urlparse(full).netloc) != base_dom:
            continue
        if full.rstrip("/") == base_url.rstrip("/"):
            continue
        if any(w in anchor for w in TEAM_LINK_WORDS) and full not in team_urls:
            team_urls.append(full)
        elif any(w in anchor for w in CONTACT_LINK_WORDS) and full not in contact_urls:
            contact_urls.append(full)

    picked = (team_urls[:2] + contact_urls[:1])[:3]
    return picked


async def _fetch_text(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Вернуть `(decoded_html, final_url)` или `('', url)` при сетевой ошибке."""
    try:
        resp = await client.get(url)
        if resp.status_code >= 400:
            return "", str(resp.url)
        return decode_russian_bytes(resp.content), str(resp.url)
    except (httpx.HTTPError, UnsafeURLError, OSError) as exc:
        logger.debug("Не удалось прочитать %s: %s", url, exc)
        return "", url


async def apply_site_analysis_to_state(
    state: StateManager,
    company: Company,
    analysis: dict,
    *,
    evidence_url: str,
    run_id: str = "",
) -> tuple[int, int]:
    """Записать результаты разбора сайта в `companies`, `observations` и `prospects`.

    Возвращает `(observations_written, prospects_created)`.
    """
    obs_written = 0
    new_prospects = 0

    human_signals: list[str] = []
    if not analysis["has_metrika"]:
        human_signals.append("Нет Яндекс.Метрики")
    if analysis["paid_tools"]:
        human_signals.append("Платный трафик: " + ", ".join(analysis["paid_tools"]))
    if analysis["missing_practice_pages"]:
        human_signals.append("Нет отдельных посадочных под практики")
    if analysis["weak_transparency"]:
        human_signals.append("Не указаны реестровые номера / кейсы")
    if analysis["compliance_risks"]:
        human_signals.append("Риски ФЗ-38/КПЭА на сайте")
    if not analysis["has_fast_messenger"]:
        human_signals.append("Нет быстрого мессенджера на сайте")

    await state.update_company_profile_fields(
        company.id,
        name=analysis["site_title"] if not company.name else "",
        legal_form=analysis["legal_form"],
        description=analysis["description"],
        phone=analysis["phone"],
        telegram=analysis["telegram"],
        whatsapp=analysis["whatsapp"],
        vk=analysis["vk"],
        tenchat=analysis["tenchat"],
        tech_stack=analysis["tech_stack"],
        signals=human_signals,
    )

    # Наблюдения профиля
    candidates: list[tuple[str, str, float | None, str, float]] = []
    if not analysis["has_metrika"]:
        candidates.append(("NO_YANDEX_METRIKA", "profile", 1.0, "Код Метрики не найден", 1.0))
    if analysis["paid_tools"]:
        candidates.append(("RUNNING_YANDEX_DIRECT", "profile", None, ", ".join(analysis["paid_tools"]), 1.0))
    if analysis["tech_stack"]:
        candidates.append(("TECH_STACK_RU", "profile", None, ", ".join(analysis["tech_stack"]), 1.0))
    if analysis["missing_practice_pages"]:
        candidates.append(("MISSING_PRACTICE_PAGES", "profile", 1.0, "Менее 2 профильных разделов практик", 1.0))
    if analysis["weak_transparency"]:
        candidates.append(("WEAK_TRUST_TRANSPARENCY", "profile", 1.0, "Нет реестровых номеров или раздела практики", 1.0))
    if analysis["compliance_risks"]:
        candidates.append(("COMPLIANCE_RISK_ON_SITE", "profile", None, "; ".join(analysis["compliance_risks"]), 0.85))
    if not analysis["has_fast_messenger"]:
        candidates.append(("NO_FAST_MESSENGER_CTA", "profile", 1.0, "Нет ссылок t.me / wa.me", 1.0))
    if not analysis["has_schema"]:
        candidates.append(("NO_SCHEMA_MARKUP", "profile", 1.0, "Нет JSON-LD Schema.org", 1.0))
    if analysis["ssl_or_mobile_issues"]:
        candidates.append(("NO_SSL_OR_MOBILE_ISSUES", "profile", 1.0, "Нет HTTPS или meta viewport", 1.0))
    if analysis["page_count"] is not None:
        candidates.append(("SITE_PAGE_COUNT", "profile", float(analysis["page_count"]), f"{analysis['page_count']} стр.", 1.0))
    if analysis["stale_days"] is not None and analysis["stale_days"] >= 180:
        candidates.append(("BLOG_STALE", "profile", float(analysis["stale_days"]), f"{analysis['stale_days']} дн. без обновлений", 1.0))
    if analysis["incumbent_agency"]:
        candidates.append(("INCUMBENT_AGENCY", "profile", None, analysis["incumbent_agency"], 0.85))
    if analysis["telegram"]:
        candidates.append(("TELEGRAM_CONTACT", "profile", None, analysis["telegram"], 1.0))
    if analysis["public_email"]:
        candidates.append(("PUBLIC_EMAIL_CONTACT", "profile", None, analysis["public_email"], 1.0))
    if analysis["contact_form_url"]:
        candidates.append(("CONTACT_FORM_URL", "profile", None, analysis["contact_form_url"], 1.0))

    for code, coll, vnum, vtext, conf in candidates:
        obs_id = await state.record_observation(
            signal_code=code,
            company_id=company.id,
            collector=coll,
            value_num=vnum,
            value_text=vtext,
            confidence=conf,
            evidence_url=evidence_url,
            run_id=run_id,
        )
        if obs_id:
            obs_written += 1

    # Сохраняем до 2 главных ЛПР (или общий контакт практики, если на сайте нет имён, но есть TG/Email)
    people = analysis["people"][:2]
    if people:
        for person in people:
            full_fio = " ".join(p for p in (person["last_name"], person["first_name"], person["middle_name"]) if p)
            obs_id = await state.record_observation(
                signal_code="DECISION_MAKER_FOUND",
                company_id=company.id,
                collector="people",
                value_text=f"{full_fio} — {person['title']}",
                evidence_url=person.get("evidence_url") or evidence_url,
                run_id=run_id,
            )
            if obs_id:
                obs_written += 1
            if person.get("registry_number"):
                await state.record_observation(
                    signal_code="MINJUST_REGISTRY_ACTIVE",
                    company_id=company.id,
                    collector="discover",
                    value_text=f"{person['registry_number']} ({full_fio})",
                    evidence_url=person.get("evidence_url") or evidence_url,
                    run_id=run_id,
                )
            _, is_new = await state.upsert_prospect(
                Prospect(
                    company_id=company.id,
                    last_name=person["last_name"],
                    first_name=person["first_name"],
                    middle_name=person["middle_name"],
                    title=person["title"],
                    role_kind=person["role_kind"],
                    registry_number=person.get("registry_number", ""),
                    registry_status="действующий" if person.get("registry_number") else "",
                    email=analysis["public_email"],
                    email_source="website_mailto" if analysis["public_email"] else "",
                    phone=analysis["phone"] or company.phone,
                    telegram=analysis["telegram"] or company.telegram,
                    whatsapp=analysis["whatsapp"] or company.whatsapp,
                    tenchat=analysis["tenchat"],
                    vk=analysis["vk"] or company.vk,
                    company=company.name,
                    city=company.city,
                    source="site_people",
                    source_url=person.get("evidence_url") or evidence_url,
                )
            )
            if is_new:
                new_prospects += 1
    elif analysis["telegram"] or analysis["public_email"] or company.telegram:
        _, is_new = await state.upsert_prospect(
            Prospect(
                company_id=company.id,
                title="Общий контакт практики",
                role_kind="general",
                email=analysis["public_email"],
                email_source="website_mailto" if analysis["public_email"] else "",
                phone=analysis["phone"] or company.phone,
                telegram=analysis["telegram"] or company.telegram,
                whatsapp=analysis["whatsapp"] or company.whatsapp,
                company=company.name,
                city=company.city,
                source="company_contact",
                source_url=evidence_url,
            )
        )
        if is_new:
            new_prospects += 1

    await state.mark_prospects_checked(company.id)
    return obs_written, new_prospects


async def profile_companies(
    state: StateManager,
    companies: list[Company],
    *,
    config: FridayConfig | None = None,
    concurrency: int = 5,
) -> tuple[int, int, str]:
    """Профилировать пакет сайтов практик параллельно.

    Возвращает `(observations_written, prospects_created, run_id)`.
    """
    if not companies:
        return 0, 0, ""

    run_id = await state.start_run(
        stage="profile",
        provider="http_ru",
        params={"companies": len(companies)},
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    total_obs = 0
    total_prospects = 0

    async with public_http_client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        async def _one(comp: Company) -> tuple[int, int]:
            async with sem:
                start_url = comp.website or (f"https://{display_domain(comp.domain)}" if comp.domain else "")
                if not start_url:
                    await state.mark_profile_checked(comp.id)
                    return 0, 0

                html, final_url = await _fetch_text(client, start_url)
                if not html and start_url.startswith("https://"):
                    html, final_url = await _fetch_text(client, "http://" + start_url[len("https://") :])

                if not html:
                    await state.mark_profile_checked(comp.id)
                    return 0, 0

                sitemap_url = urljoin(final_url, "/sitemap.xml")
                sitemap_xml, _ = await _fetch_text(client, sitemap_url)

                extra_pages: list[tuple[str, str]] = []
                for sub_url in _find_internal_subpages(html, final_url):
                    sub_html, sub_final = await _fetch_text(client, sub_url)
                    if sub_html:
                        extra_pages.append((sub_final, sub_html))

                analysis = analyze_practice_site_html(
                    html,
                    base_url=final_url,
                    sitemap_xml=sitemap_xml,
                    extra_pages_html=extra_pages,
                )
                return await apply_site_analysis_to_state(
                    state, comp, analysis, evidence_url=final_url, run_id=run_id
                )

        results = await asyncio.gather(*(_one(c) for c in companies), return_exceptions=True)
        for res in results:
            if isinstance(res, tuple):
                total_obs += res[0]
                total_prospects += res[1]
            elif isinstance(res, Exception):
                logger.warning("Ошибка профилирования сайта: %s", res)

    await state.finish_run(run_id, status="completed", records=total_obs)
    return total_obs, total_prospects, run_id
