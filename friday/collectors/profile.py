"""Детерминированный HTTP-профайлер сайтов российских компаний ($0, SSRF-safe, любой рынок).

Проверяет и записывает в `observations` универсальные сигналы Рунета:
- Доступность, скорость ответа, HTTPS/SSL и мобильный `viewport`
- Яндекс.Метрика, Яндекс.Директ, Пиксель VK Рекламы
- Коллтрекинг и сквозная аналитика (Calltouch, Roistat, CoMagic/UIS, Mango Office)
- Виджеты захвата, онлайн-записи и CRM (Битрикс24, amoCRM, Jivo, Marquiz, Envybox, YCLIENTS, DIKIDI, Bnovo)
- CMS / E-commerce стек (Tilda, 1С-Битрикс, WordPress, MODX, InSales, OpenCart, CS-Cart, Next.js)
- Наличие онлайн-захвата (`NO_ONLINE_CAPTURE`), мессенджеров (Telegram, WhatsApp, VK, TenChat)
- Развитость структуры разделов (`THIN_SERVICE_STRUCTURE`), год копирайта и ИНН компании
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse

from friday.models.company import Company
from friday.public_http import PublicHttpError, SafePublicHttpClient
from friday.state import StateManager

METRIKA_RE = re.compile(r"mc\.yandex\.ru/metrika|ym\(\s*\d{5,10}", re.IGNORECASE)
YANDEX_DIRECT_RE = re.compile(r"yandex_context|yaContextCb|an\.yandex\.ru/system/context", re.IGNORECASE)
VK_PIXEL_RE = re.compile(r"vk\.com/js/api/openapi\.js|VK\.Retargeting|top-fwz1\.mail\.ru", re.IGNORECASE)

CALLTRACKING_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Calltouch", re.compile(r"calltouch\.ru|mod\.calltouch", re.IGNORECASE)),
    ("Roistat", re.compile(r"cloud\.roistat\.com|roistat_visit", re.IGNORECASE)),
    ("CoMagic/UIS", re.compile(r"comagic\.ru|uiscom\.ru", re.IGNORECASE)),
    ("Mango Office", re.compile(r"widgets\.mango-office\.ru|mangotele\.com", re.IGNORECASE)),
]

CRM_WIDGET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Битрикс24", re.compile(r"cdn-ru\.bitrix24\.ru|bitrix24\.ru/b\d+", re.IGNORECASE)),
    ("amoCRM", re.compile(r"amocrm\.ru|amoforms\.ru|gso\.amocrm", re.IGNORECASE)),
    ("Jivo", re.compile(r"code\.jivo\.ru|jivosite\.com", re.IGNORECASE)),
    ("Marquiz", re.compile(r"marquiz\.ru", re.IGNORECASE)),
    ("Envybox", re.compile(r"envybox\.io|callbackkiller", re.IGNORECASE)),
    ("Carrot quest", re.compile(r"carrotquest\.io", re.IGNORECASE)),
    ("Talk-Me", re.compile(r"talk-me\.ru", re.IGNORECASE)),
]

ONLINE_BOOKING_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("YCLIENTS", re.compile(r"yclients\.com", re.IGNORECASE)),
    ("DIKIDI", re.compile(r"dikidi\.net|dikidi\.ru", re.IGNORECASE)),
    ("ПроДокторов / MedFlex", re.compile(r"medflex\.ru|prodoctorov\.ru/widgets", re.IGNORECASE)),
    ("Bnovo / TravelLine", re.compile(r"bnovo\.ru|travelline\.ru", re.IGNORECASE)),
]

CMS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Tilda", re.compile(r"tildacdn\.com|tilda\.ws|t-records", re.IGNORECASE)),
    ("1С-Битрикс", re.compile(r"/bitrix/templates/|/bitrix/js/|bx\.ajax", re.IGNORECASE)),
    ("InSales", re.compile(r"insales\.ru|assets\.insales", re.IGNORECASE)),
    ("WordPress", re.compile(r"/wp-content/|/wp-includes/", re.IGNORECASE)),
    ("MODX", re.compile(r"assets/components/|assets/templates/", re.IGNORECASE)),
    ("OpenCart", re.compile(r"catalog/view/theme/|index\.php\?route=", re.IGNORECASE)),
    ("CS-Cart", re.compile(r"design/themes/|var/cache/misc", re.IGNORECASE)),
    ("Next.js", re.compile(r"/_next/static/", re.IGNORECASE)),
    ("Nuxt", re.compile(r"/_nuxt/", re.IGNORECASE)),
    ("Webflow", re.compile(r"webflow\.com|w-webflow-badge", re.IGNORECASE)),
]

VIEWPORT_RE = re.compile(r'<meta[^>]+name=["\']viewport["\']', re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
FORM_RE = re.compile(r"<form\b", re.IGNORECASE)

TELEGRAM_LINK_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/([a-zA-Z0-9_]{4,32})(?![a-zA-Z0-9_])",
    re.IGNORECASE,
)
WHATSAPP_LINK_RE = re.compile(
    r"https?://(?:wa\.me|api\.whatsapp\.com/send\?phone=)(\+?\d{10,15})",
    re.IGNORECASE,
)
VK_LINK_RE = re.compile(r"https?://(?:m\.)?vk\.com/([a-zA-Z0-9_.-]{3,40})", re.IGNORECASE)
TENCHAT_LINK_RE = re.compile(r"https?://tenchat\.ru/([a-zA-Z0-9_.-]{3,40})", re.IGNORECASE)

RU_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
INN_RE = re.compile(r"ИНН[\s:№\-]*([0-9]{10}(?:[0-9]{2})?)\b", re.IGNORECASE)
COPYRIGHT_YEAR_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:20\d{2}\s*[-–—]\s*)?(20\d{2})", re.IGNORECASE)
INTERNAL_HREF_RE = re.compile(r'<a[^>]+href=["\'](/[^"\'#?]{2,80})["\']', re.IGNORECASE)

EXCLUDED_TG_HANDLES = {"share", "joinchat", "s", "iv", "proxy", "addstickers"}
EXCLUDED_VK_HANDLES = {"share.php", "widget", "js", "login", "feed", "im"}


def inspect_company_html(
    html: str,
    *,
    final_url: str,
    elapsed_ms: float,
    had_https_redirect: bool = True,
    current_year: int | None = None,
) -> dict:
    """Чистая детерминированная функция разбора HTML-страницы любой российской компании."""
    year_now = current_year or datetime.now(timezone.utc).year
    tech_stack: list[str] = []
    observations: list[dict] = []

    if elapsed_ms > 3500:
        observations.append(
            {
                "signal_code": "SLOW_SITE",
                "value_num": round(elapsed_ms, 0),
                "value_text": f"Время ответа главной страницы {int(elapsed_ms)} мс",
                "evidence_url": final_url,
            }
        )

    if not had_https_redirect:
        observations.append(
            {
                "signal_code": "NO_HTTPS_REDIRECT",
                "value_text": "HTTP-версия не перенаправляет на HTTPS",
                "evidence_url": final_url,
            }
        )

    if not VIEWPORT_RE.search(html):
        observations.append(
            {
                "signal_code": "NO_MOBILE_VIEWPORT",
                "value_text": "Отсутствует мета-тег viewport (сайт не адаптирован под смартфоны)",
                "evidence_url": final_url,
            }
        )

    if METRIKA_RE.search(html):
        tech_stack.append("Яндекс.Метрика")
    else:
        observations.append(
            {
                "signal_code": "NO_YANDEX_METRIKA",
                "value_text": "На главной странице не обнаружен счётчик Яндекс.Метрики",
                "evidence_url": final_url,
            }
        )

    if YANDEX_DIRECT_RE.search(html):
        tech_stack.append("Яндекс.Директ")
        observations.append(
            {
                "signal_code": "YANDEX_DIRECT_ACTIVE",
                "value_text": "Обнаружены скрипты рекламной сети Яндекса / Директа",
                "evidence_url": final_url,
            }
        )

    if VK_PIXEL_RE.search(html):
        tech_stack.append("VK Реклама")
        observations.append(
            {
                "signal_code": "VK_PIXEL_ACTIVE",
                "value_text": "Установлен пиксель ретаргетинга VK Рекламы",
                "evidence_url": final_url,
            }
        )

    calltrackers: list[str] = []
    for label, pat in CALLTRACKING_PATTERNS:
        if pat.search(html):
            calltrackers.append(label)
            tech_stack.append(label)
    if calltrackers:
        observations.append(
            {
                "signal_code": "CALLTRACKING_ACTIVE",
                "value_text": ", ".join(calltrackers),
                "evidence_url": final_url,
            }
        )

    crm_widgets: list[str] = []
    for label, pat in CRM_WIDGET_PATTERNS:
        if pat.search(html):
            crm_widgets.append(label)
            tech_stack.append(label)
    if crm_widgets:
        observations.append(
            {
                "signal_code": "CRM_WIDGET_ACTIVE",
                "value_text": ", ".join(crm_widgets),
                "evidence_url": final_url,
            }
        )

    booking_widgets: list[str] = []
    for label, pat in ONLINE_BOOKING_PATTERNS:
        if pat.search(html):
            booking_widgets.append(label)
            tech_stack.append(label)
    if booking_widgets:
        observations.append(
            {
                "signal_code": "ONLINE_BOOKING_ACTIVE",
                "value_text": ", ".join(booking_widgets),
                "evidence_url": final_url,
            }
        )

    cms_found: list[str] = []
    for label, pat in CMS_PATTERNS:
        if pat.search(html):
            cms_found.append(label)
            tech_stack.append(label)
    if cms_found:
        observations.append(
            {
                "signal_code": "TECH_STACK_RU",
                "value_text": ", ".join(cms_found),
                "evidence_url": final_url,
            }
        )

    telegram = ""
    for m in TELEGRAM_LINK_RE.finditer(html):
        handle = m.group(1)
        if handle.lower() not in EXCLUDED_TG_HANDLES:
            telegram = f"https://t.me/{handle}"
            break

    whatsapp = ""
    wm = WHATSAPP_LINK_RE.search(html)
    if wm:
        whatsapp = f"https://wa.me/{wm.group(1).lstrip('+')}"

    vk = ""
    for m in VK_LINK_RE.finditer(html):
        handle = m.group(1)
        if handle.lower() not in EXCLUDED_VK_HANDLES:
            vk = f"https://vk.com/{handle}"
            break

    tenchat = ""
    tm = TENCHAT_LINK_RE.search(html)
    if tm:
        tenchat = f"https://tenchat.ru/{tm.group(1)}"

    if telegram:
        observations.append(
            {
                "signal_code": "TELEGRAM_PUBLIC",
                "value_text": telegram,
                "evidence_url": final_url,
            }
        )
    if whatsapp:
        observations.append(
            {
                "signal_code": "WHATSAPP_PUBLIC",
                "value_text": whatsapp,
                "evidence_url": final_url,
            }
        )
    if not telegram and not whatsapp and not crm_widgets:
        observations.append(
            {
                "signal_code": "NO_MESSENGER_WIDGET",
                "value_text": "На главной странице нет ссылок на Telegram/WhatsApp и онлайн-чата",
                "evidence_url": final_url,
            }
        )

    has_form = bool(FORM_RE.search(html))
    if not has_form and not crm_widgets and not booking_widgets and not telegram and not whatsapp:
        observations.append(
            {
                "signal_code": "NO_ONLINE_CAPTURE",
                "value_text": "На главной нет форм заявки, квиза, онлайн-чата и кнопок мессенджеров",
                "evidence_url": final_url,
            }
        )

    years = [int(y) for y in COPYRIGHT_YEAR_RE.findall(html) if 2005 <= int(y) <= year_now]
    if years and max(years) <= year_now - 2:
        observations.append(
            {
                "signal_code": "OUTDATED_COPYRIGHT",
                "value_num": float(max(years)),
                "value_text": f"В футере указан устаревший © {max(years)} год",
                "evidence_url": final_url,
            }
        )

    # Проверяем глубину структуры разделов (каталог / услуги / направления)
    unique_paths = {
        m.group(1).rstrip("/").lower()
        for m in INTERNAL_HREF_RE.finditer(html)
        if not m.group(1).lower().endswith((".jpg", ".png", ".svg", ".pdf", ".css", ".js"))
    }
    if len(unique_paths) < 3:
        observations.append(
            {
                "signal_code": "THIN_SERVICE_STRUCTURE",
                "value_num": float(len(unique_paths)),
                "value_text": f"Одностраничная или узкая структура ({len(unique_paths)} внутренних ссылок) без отдельных посадочных под услуги/товары",
                "evidence_url": final_url,
            }
        )

    title_m = TITLE_RE.search(html)
    title = re.sub(r"\s+", " ", unescape(title_m.group(1))).strip()[:180] if title_m else ""
    desc_m = META_DESC_RE.search(html)
    description = re.sub(r"\s+", " ", unescape(desc_m.group(1))).strip()[:300] if desc_m else ""

    phone_m = RU_PHONE_RE.search(html)
    phone = phone_m.group(0).strip() if phone_m else ""

    inn_m = INN_RE.search(html)
    inn = inn_m.group(1) if inn_m else ""

    return {
        "title": title,
        "description": description,
        "phone": phone,
        "inn": inn,
        "telegram": telegram,
        "whatsapp": whatsapp,
        "vk": vk,
        "tenchat": tenchat,
        "tech_stack": list(dict.fromkeys(tech_stack)),
        "observations": observations,
    }


async def profile_single_company(
    state: StateManager,
    company: Company,
    client: SafePublicHttpClient,
    *,
    run_id: str = "",
) -> dict:
    raw_url = company.website or (f"https://{company.domain}" if company.domain else "")
    if not raw_url:
        await state.mark_profile_checked(company.id)
        return {"company_id": company.id, "ok": False, "reason": "no_url"}

    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    host = parsed.netloc or parsed.path
    https_url = f"https://{host}"
    http_url = f"http://{host}"

    had_https_redirect = True
    try:
        http_resp = await client.get(http_url)
        had_https_redirect = str(http_resp.url).startswith("https://")
    except PublicHttpError:
        pass

    start = asyncio.get_running_loop().time()
    try:
        resp = await client.get(https_url)
    except PublicHttpError as https_err:
        err_str = str(https_err).lower()
        if "ssl" in err_str or "certificate" in err_str or "tls" in err_str:
            await state.record_observation(
                signal_code="SSL_ISSUE",
                company_id=company.id,
                collector="profile",
                value_text=f"Ошибка SSL/HTTPS: {str(https_err)[:140]}",
                evidence_url=https_url,
                run_id=run_id,
            )
            try:
                resp = await client.get(http_url)
                had_https_redirect = False
            except PublicHttpError as http_err:
                await state.record_observation(
                    signal_code="SITE_UNREACHABLE",
                    company_id=company.id,
                    collector="profile",
                    value_text="Сайт недоступен по сети",
                    evidence_url=http_url,
                    run_id=run_id,
                )
                await state.mark_profile_checked(company.id)
                return {"company_id": company.id, "ok": False, "reason": "unreachable"}
        else:
            await state.record_observation(
                signal_code="SITE_UNREACHABLE",
                company_id=company.id,
                collector="profile",
                value_text="Сайт недоступен по сети",
                evidence_url=https_url,
                run_id=run_id,
            )
            await state.mark_profile_checked(company.id)
            return {"company_id": company.id, "ok": False, "reason": "unreachable"}

    elapsed_ms = (asyncio.get_running_loop().time() - start) * 1000.0
    if resp.status_code >= 400:
        await state.record_observation(
            signal_code="SITE_UNREACHABLE",
            company_id=company.id,
            collector="profile",
            value_num=float(resp.status_code),
            value_text=f"HTTP статус {resp.status_code}",
            evidence_url=str(resp.url),
            run_id=run_id,
        )
        await state.mark_profile_checked(company.id)
        return {"company_id": company.id, "ok": False, "reason": f"http_{resp.status_code}"}

    html = resp.text[:350_000]
    info = inspect_company_html(
        html,
        final_url=str(resp.url),
        elapsed_ms=elapsed_ms,
        had_https_redirect=had_https_redirect,
    )

    sig_codes = [o["signal_code"] for o in info["observations"]]
    await state.update_company_profile_fields(
        company.id,
        name=info["title"] if not company.name or "." in company.name else "",
        inn=info["inn"],
        description=info["description"],
        phone=info["phone"],
        telegram=info["telegram"],
        whatsapp=info["whatsapp"],
        vk=info["vk"],
        tenchat=info["tenchat"],
        tech_stack=info["tech_stack"],
        signals=sig_codes,
    )

    recorded = 0
    for obs in info["observations"]:
        oid = await state.record_observation(
            signal_code=obs["signal_code"],
            company_id=company.id,
            collector="profile",
            value_num=obs.get("value_num"),
            value_text=obs.get("value_text", ""),
            evidence_url=obs.get("evidence_url", str(resp.url)),
            run_id=run_id,
        )
        if oid:
            recorded += 1

    return {"company_id": company.id, "ok": True, "observations": recorded}


async def run_profile(
    state: StateManager,
    *,
    limit: int = 50,
    concurrency: int = 8,
    stale_days: int = 90,
) -> dict:
    companies = await state.companies_needing_profile(limit=limit, stale_days=stale_days)
    if not companies:
        return {"checked": 0, "ok": 0, "observations": 0}

    run_id = await state.start_run(
        stage="profile",
        provider="http_ru",
        params={"limit": limit, "count": len(companies)},
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    client = SafePublicHttpClient(timeout=12.0)

    async def _worker(comp: Company) -> dict:
        async with sem:
            return await profile_single_company(state, comp, client, run_id=run_id)

    try:
        results = await asyncio.gather(*(_worker(c) for c in companies))
        ok_count = sum(1 for r in results if r.get("ok"))
        obs_count = sum(int(r.get("observations", 0)) for r in results)
        await state.finish_run(run_id, status="completed", records=ok_count)
        return {"checked": len(results), "ok": ok_count, "observations": obs_count}
    except Exception as exc:
        await state.finish_run(run_id, status="failed", error=str(exc))
        raise
    finally:
        await client.close()
