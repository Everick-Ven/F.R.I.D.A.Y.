"""Поиск ЛПР и публичных контактов на сайтах российских компаний (`prospects`, любая ниша).

Детерминированный краулер ($0, без LLM, SSRF-safe):
1. Обходит главную и внутренние страницы (`/about`, `/o-kompanii`, `/team`, `/komanda`, `/rukovodstvo`, `/contacts`, `/rekvizity`).
2. Извлекает полные русские ФИО («Фамилия Имя Отчество» и «ИП Фамилия Имя Отчество»).
3. Сопоставляет должности ЛПР с универсальным словарём РФ и `icp.decision_maker_titles` из конфигурации ниши.
4. Отделяет персональные/прямые e-mail от общих ящиков (`info@`, `sales@`, `zakaz@`).
5. Записывает сигналы `DECISION_MAKER_FOUND` и `PERSONAL_EMAIL_FOUND`.
"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from urllib.parse import urljoin, urlparse

from friday.config import FridayConfig, load_config
from friday.gate import normalize_domain_idna
from friday.models.company import Company
from friday.models.prospect import Prospect
from friday.public_http import PublicHttpError, SafePublicHttpClient
from friday.state import StateManager

TEAM_PATH_HINTS = (
    "team",
    "komanda",
    "staff",
    "rukovodstvo",
    "management",
    "leadership",
    "about",
    "o-kompanii",
    "o-nas",
    "company",
    "contacts",
    "kontakty",
    "rekvizity",
    "specialists",
    "doctors",
    "vrachi",
    "partners",
)

# Морфология полных русских ФИО: «Иванов Иван Иванович»
FIO_FULL_RE = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,24}(?:-[А-ЯЁ][а-яё]{2,20})?)\s+"
    r"([А-ЯЁ][а-яё]{2,18})\s+"
    r"([А-ЯЁ][а-яё]{2,20}(?:ович|евич|ич|овна|евна|ична|ьич|инична))\b"
)

# Формат реквизитов ИП: «Индивидуальный предприниматель Иванов Иван Иванович» / «ИП Иванов Иван Иванович»
IP_FIO_RE = re.compile(
    r"(?:Индивидуальный\s+предприниматель|ИП)\s+"
    r"([А-ЯЁ][а-яё]{2,24})\s+([А-ЯЁ][а-яё]{2,18})(?:\s+([А-ЯЁ][а-яё]{2,22}))?"
)

BASE_TITLES_RU: list[tuple[str, str]] = [
    ("генеральный директор", "ceo"),
    ("коммерческий директор", "commercial_director"),
    ("исполнительный директор", "coo"),
    ("директор по развитию", "bizdev_director"),
    ("директор по маркетингу", "cmo"),
    ("директор по закупкам", "procurement_director"),
    ("директор по логистике", "logistics_director"),
    ("технический директор", "cto"),
    ("финансовый директор", "cfo"),
    ("руководитель отдела продаж", "head_of_sales"),
    ("руководитель отдела маркетинга", "head_of_marketing"),
    ("руководитель отдела закупок", "head_of_procurement"),
    ("управляющий партнёр", "managing_partner"),
    ("управляющий партнер", "managing_partner"),
    ("управляющий", "general_manager"),
    ("основатель", "founder"),
    ("сооснователь", "founder"),
    ("учредитель", "owner"),
    ("собственник", "owner"),
    ("владелец", "owner"),
    ("главный врач", "chief_medical_officer"),
    ("главный инженер", "chief_engineer"),
    ("директор", "director"),
    ("индивидуальный предприниматель", "sole_proprietor"),
]

EMAIL_RE = re.compile(r"\b([a-zA-Z0-9._%+-]{1,64}@[a-zA-Z0-9.-]{2,120}\.[a-zA-Zа-яА-ЯрфРФ]{2,16})\b")
GENERIC_EMAIL_PREFIXES = {
    "info", "mail", "office", "contact", "contacts", "hello", "support",
    "admin", "noreply", "no-reply", "reception", "sekretar", "secretary",
    "sales", "sale", "zakaz", "order", "orders", "buh", "accounting",
    "hr", "job", "press", "pr", "marketing", "opt", "tender",
}
JUNK_EMAIL_DOMAINS = {
    "sentry.io", "example.com", "domain.ru", "test.ru", "yoursite.ru", "tilda.ws", "2x.png",
}

TELEGRAM_LINK_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/([a-zA-Z0-9_]{4,32})(?![a-zA-Z0-9_])",
    re.IGNORECASE,
)
WHATSAPP_LINK_RE = re.compile(
    r"https?://(?:wa\.me|api\.whatsapp\.com/send\?phone=)(\+?\d{10,15})",
    re.IGNORECASE,
)
TENCHAT_LINK_RE = re.compile(r"https?://tenchat\.ru/([a-zA-Z0-9_.-]{3,40})", re.IGNORECASE)
RU_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
HREF_RE = re.compile(r'<a[^>]+href=["\']([^"\'#]+)["\']', re.IGNORECASE)

STOP_SURNAMES = {
    "Российская", "Федерация", "Политика", "Конфиденциальности", "Пользовательское",
    "Соглашение", "Персональных", "Данных", "Общество", "Ограниченной", "Ответственностью",
    "Акционерное", "Публичное", "Юридический", "Фактический", "Генеральный", "Коммерческий",
    "Исполнительный", "Официальный", "Главная", "Услуги", "Каталог", "Контакты",
}


def _strip_tags(html: str) -> str:
    cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", unescape(cleaned)).strip()


def _detect_title_near(
    text: str,
    start: int,
    end: int,
    custom_titles: list[str] | None = None,
) -> tuple[str, str]:
    window = text[max(0, start - 130) : min(len(text), end + 130)].lower()
    for custom in custom_titles or []:
        cl = custom.strip().lower()
        if cl and cl in window:
            return custom.strip().capitalize(), "icp_decision_maker"
    for phrase, kind in BASE_TITLES_RU:
        if phrase in window:
            return phrase.capitalize(), kind
    return "Руководитель / представитель", "decision_maker"


def extract_people_from_html(
    html: str,
    *,
    page_url: str,
    company: Company,
    custom_titles: list[str] | None = None,
) -> list[Prospect]:
    text = _strip_tags(html)

    emails: list[str] = []
    for em in EMAIL_RE.findall(html):
        em_l = em.lower().strip(".")
        if any(em_l.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".js", ".css")):
            continue
        dom = em_l.split("@", 1)[-1]
        if dom in JUNK_EMAIL_DOMAINS:
            continue
        if em_l not in emails:
            emails.append(em_l)

    personal_emails = [e for e in emails if e.split("@", 1)[0] not in GENERIC_EMAIL_PREFIXES]
    generic_emails = [e for e in emails if e.split("@", 1)[0] in GENERIC_EMAIL_PREFIXES]

    tg_m = TELEGRAM_LINK_RE.search(html)
    tg = f"https://t.me/{tg_m.group(1)}" if tg_m and tg_m.group(1).lower() not in {"share", "s", "joinchat"} else company.telegram

    wa_m = WHATSAPP_LINK_RE.search(html)
    wa = f"https://wa.me/{wa_m.group(1).lstrip('+')}" if wa_m else company.whatsapp

    tc_m = TENCHAT_LINK_RE.search(html)
    tc = f"https://tenchat.ru/{tc_m.group(1)}" if tc_m else company.tenchat

    ph_m = RU_PHONE_RE.search(html)
    phone = ph_m.group(0).strip() if ph_m else company.phone

    seen_fio: set[tuple[str, str]] = set()
    prospects: list[Prospect] = []

    # 1. Ищем ИП в реквизитах
    for ip_m in IP_FIO_RE.finditer(text):
        last_n, first_n = ip_m.group(1), ip_m.group(2)
        mid_n = ip_m.group(3) or ""
        if last_n in STOP_SURNAMES or first_n in STOP_SURNAMES:
            continue
        key = (last_n.lower(), first_n.lower())
        if key in seen_fio:
            continue
        seen_fio.add(key)
        assigned_email = personal_emails[0] if personal_emails else (generic_emails[0] if generic_emails else "")
        prospects.append(
            Prospect(
                company_id=company.id,
                last_name=last_n,
                first_name=first_n,
                middle_name=mid_n,
                title="Индивидуальный предприниматель (собственник)",
                role_kind="sole_proprietor",
                email=assigned_email,
                email_source="personal_page" if assigned_email in personal_emails else ("office_generic" if assigned_email else ""),
                phone=phone,
                telegram=tg,
                whatsapp=wa,
                tenchat=tc,
                vk=company.vk,
                company=company.name,
                industry=company.industry,
                city=company.city,
                source="website_team",
                source_url=page_url,
            )
        )

    # 2. Ищем полные ФИО руководителей и профильных ЛПР
    for match in FIO_FULL_RE.finditer(text):
        last_n, first_n, mid_n = match.group(1), match.group(2), match.group(3)
        if last_n in STOP_SURNAMES or first_n in STOP_SURNAMES:
            continue
        key = (last_n.lower(), first_n.lower())
        if key in seen_fio:
            continue
        seen_fio.add(key)

        title, role_kind = _detect_title_near(text, match.start(), match.end(), custom_titles=custom_titles)

        idx = len(prospects)
        assigned_email = ""
        email_source = ""
        if idx < len(personal_emails):
            assigned_email = personal_emails[idx]
            email_source = "personal_page"
        elif idx == 0 and generic_emails:
            assigned_email = generic_emails[0]
            email_source = "office_generic"

        prospects.append(
            Prospect(
                company_id=company.id,
                last_name=last_n,
                first_name=first_n,
                middle_name=mid_n,
                title=title,
                role_kind=role_kind,
                email=assigned_email,
                email_source=email_source,
                phone=phone,
                telegram=tg,
                whatsapp=wa,
                tenchat=tc,
                vk=company.vk,
                company=company.name,
                industry=company.industry,
                city=company.city,
                source="website_team",
                source_url=page_url,
            )
        )
        if len(prospects) >= 5:
            break

    return prospects


def find_candidate_subpages(home_html: str, base_url: str) -> list[str]:
    base_dom = normalize_domain_idna(base_url)
    found: list[str] = []
    for m in HREF_RE.finditer(home_html):
        href = m.group(1).strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        if normalize_domain_idna(full) != base_dom:
            continue
        path_l = urlparse(full).path.lower()
        if any(hint in path_l for hint in TEAM_PATH_HINTS):
            if full not in found and full.rstrip("/") != base_url.rstrip("/"):
                found.append(full)
        if len(found) >= 4:
            break
    return found


async def enrich_company_people(
    state: StateManager,
    company: Company,
    client: SafePublicHttpClient,
    *,
    config: FridayConfig,
    run_id: str = "",
) -> int:
    base_url = company.website or (f"https://{company.domain}" if company.domain else "")
    created_count = 0
    custom_titles = config.icp.decision_maker_titles

    if base_url:
        if "://" not in base_url:
            base_url = f"https://{base_url}"
        try:
            home_resp = await client.get(base_url)
            if home_resp.status_code < 400:
                home_html = home_resp.text[:300_000]
                pages_to_scan = [(str(home_resp.url), home_html)]
                for sub_url in find_candidate_subpages(home_html, str(home_resp.url)):
                    try:
                        sub_resp = await client.get(sub_url)
                        if sub_resp.status_code < 400:
                            pages_to_scan.append((str(sub_resp.url), sub_resp.text[:300_000]))
                    except PublicHttpError:
                        continue

                all_found: list[Prospect] = []
                seen_names: set[tuple[str, str]] = set()
                for purl, phtml in pages_to_scan:
                    for p in extract_people_from_html(
                        phtml, page_url=purl, company=company, custom_titles=custom_titles
                    ):
                        k = (p.last_name.lower(), p.first_name.lower())
                        if k not in seen_names:
                            seen_names.add(k)
                            all_found.append(p)

                for p in all_found[:5]:
                    pid, created = await state.upsert_prospect(p)
                    if created:
                        created_count += 1
                        await state.record_observation(
                            signal_code="DECISION_MAKER_FOUND",
                            company_id=company.id,
                            prospect_id=pid,
                            collector="people",
                            value_text=f"{p.full_name} ({p.title})",
                            evidence_url=p.source_url,
                            run_id=run_id,
                        )
                        if p.email and p.email_source == "personal_page":
                            await state.record_observation(
                                signal_code="PERSONAL_EMAIL_FOUND",
                                company_id=company.id,
                                prospect_id=pid,
                                collector="people",
                                value_text=p.email,
                                evidence_url=p.source_url,
                                run_id=run_id,
                            )
        except PublicHttpError:
            pass

    existing = await state.get_prospects(company_id=company.id, limit=5)
    if not existing and (company.telegram or company.whatsapp or company.phone):
        fallback = Prospect(
            company_id=company.id,
            title="Руководитель / отдел развития",
            role_kind="office_contact",
            phone=company.phone,
            telegram=company.telegram,
            whatsapp=company.whatsapp,
            tenchat=company.tenchat,
            vk=company.vk,
            company=company.name,
            industry=company.industry,
            city=company.city,
            source="company_card",
            source_url=company.website or company.twogis_url or company.ymaps_url or company.hh_url,
        )
        _, created = await state.upsert_prospect(fallback)
        if created:
            created_count += 1

    await state.mark_prospects_checked(company.id)
    return created_count


async def run_people(
    state: StateManager,
    *,
    companies: list[Company] | None = None,
    limit: int = 50,
    concurrency: int = 6,
    config: FridayConfig | None = None,
) -> dict:
    cfg = config or load_config()
    if companies is None:
        all_c = await state.get_companies(limit=limit * 2)
        companies = [c for c in all_c if not c.prospects_checked_at][:limit]

    if not companies:
        return {"companies_scanned": 0, "prospects_created": 0}

    run_id = await state.start_run(
        stage="prospects",
        provider="website_ru",
        params={"companies": len(companies)},
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    client = SafePublicHttpClient(timeout=12.0)

    async def _worker(c: Company) -> int:
        async with sem:
            return await enrich_company_people(state, c, client, config=cfg, run_id=run_id)

    try:
        counts = await asyncio.gather(*(_worker(c) for c in companies))
        total_created = sum(counts)
        await state.finish_run(run_id, status="completed", records=total_created)
        return {"companies_scanned": len(companies), "prospects_created": total_created}
    except Exception as exc:
        await state.finish_run(run_id, status="failed", error=str(exc))
        raise
    finally:
        await client.close()
