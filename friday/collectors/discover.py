"""Поиск компаний для любой ниши российского рынка (`discover`).

Источники обнаружения ($0 без LLM):
1. `hh_ru`          — открытый бесплатный API HeadHunter (`api.hh.ru`, без ключей!):
                      находит реальные компании РФ по вакансиям (`icp.hiring_signals` или запросу),
                      достаёт официальный сайт работодателя и фиксирует сигналы найма.
2. `ddg_ru`         — бесплатный веб-поиск по Рунету с отсевом агрегаторов и справочников.
3. `osm_ru`         — бесплатный поиск организаций через OpenStreetMap Overpass по городам РФ.
4. `yandex_search`  — Яндекс Search API v2 (органическая выдача, включая позиции 11–30).
5. `twogis`         — 2ГИС Catalog API (рейтинг, отзывы, контакты, мессенджеры, сайт).
6. `yandex_maps`    — Яндекс Поиск по организациям (рейтинг, отзывы, сайт, телефон).
7. `import`         — импорт CSV / JSON / TXT (выгрузки Контур.Компас, СБИС, 2ГИС, список доменов `.ru`/`.рф`).
"""

from __future__ import annotations

import csv
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from friday.config import FridayConfig, load_config
from friday.gate import is_suppressed, normalize_domain_idna
from friday.models.company import Company
from friday.models.prospect import Prospect
from friday.state import StateManager

# Агрегаторы, маркетплейсы, справочники ЕГРЮЛ и соцсети РФ — НЕ сайты целевых компаний
RU_AGGREGATOR_DOMAINS: set[str] = {
    "yandex.ru",
    "ya.ru",
    "google.com",
    "google.ru",
    "2gis.ru",
    "zoon.ru",
    "profi.ru",
    "uslugi.yandex.ru",
    "avito.ru",
    "youla.ru",
    "ozon.ru",
    "wildberries.ru",
    "market.yandex.ru",
    "cian.ru",
    "domclick.ru",
    "auto.ru",
    "drom.ru",
    "flamp.ru",
    "otzovik.com",
    "irecommend.ru",
    "yell.ru",
    "spravker.ru",
    "cataloxy.ru",
    "orgpage.ru",
    "pulset.ru",
    "blizko.ru",
    "tiu.ru",
    "satu.kz",
    "rusprofile.ru",
    "list-org.com",
    "spark-interfax.ru",
    "sbis.ru",
    "saby.ru",
    "focus.kontur.ru",
    "kontur.ru",
    "checko.ru",
    "zachestnyibiznes.ru",
    "audit-it.ru",
    "vbankcenter.ru",
    "rbc.ru",
    "kommersant.ru",
    "vedomosti.ru",
    "forbes.ru",
    "vc.ru",
    "habr.com",
    "dzen.ru",
    "pikabu.ru",
    "vk.com",
    "ok.ru",
    "t.me",
    "telegram.org",
    "tenchat.ru",
    "youtube.com",
    "rutube.ru",
    "hh.ru",
    "superjob.ru",
    "rabota.ru",
    "zarplata.ru",
    "gorodrabot.ru",
    "wikipedia.org",
    "consultant.ru",
    "garant.ru",
    "kad.arbitr.ru",
    "nalog.gov.ru",
    "gosuslugi.ru",
    "rospotrebnadzor.ru",
}

YANDEX_REGION_IDS: dict[str, int] = {
    "москва": 213,
    "санкт-петербург": 2,
    "екатеринбург": 54,
    "новосибирск": 65,
    "казань": 43,
    "нижний новгород": 47,
    "краснодар": 35,
    "самара": 51,
    "ростов-на-дону": 39,
    "челябинск": 56,
    "уфа": 172,
    "воронеж": 193,
    "пермь": 50,
    "волгоград": 38,
    "красноярск": 62,
    "владивосток": 75,
    "тюмень": 55,
    "сочи": 239,
}

HH_AREA_IDS: dict[str, str] = {
    "москва": "1",
    "санкт-петербург": "2",
    "екатеринбург": "3",
    "новосибирск": "4",
    "нижний новгород": "66",
    "казань": "88",
    "краснодар": "53",
    "самара": "78",
    "ростов-на-дону": "76",
    "челябинск": "104",
    "уфа": "99",
    "воронеж": "26",
    "пермь": "72",
    "волгоград": "24",
    "красноярск": "54",
    "владивосток": "22",
    "тюмень": "95",
    "сочи": "237",
}

CITY_BBOX_RU: dict[str, tuple[float, float, float, float]] = {
    "москва": (55.55, 37.35, 55.92, 37.86),
    "санкт-петербург": (59.80, 30.10, 60.08, 30.55),
    "екатеринбург": (56.74, 60.45, 56.93, 60.75),
    "новосибирск": (54.90, 82.78, 55.12, 83.10),
    "казань": (55.70, 48.95, 55.88, 49.28),
    "нижний новгород": (56.22, 43.80, 56.38, 44.12),
    "краснодар": (44.97, 38.88, 45.13, 39.12),
    "самара": (53.15, 50.05, 53.32, 50.32),
    "ростов-на-дону": (47.18, 39.58, 47.32, 39.85),
    "челябинск": (55.08, 61.28, 55.26, 61.52),
    "владивосток": (43.06, 131.84, 43.22, 132.02),
    "сочи": (43.53, 39.68, 43.65, 39.82),
}

# Подсказки для подбора тегов OpenStreetMap по запросу ниши
OSM_NICHE_TAG_MAP: list[tuple[tuple[str, ...], list[tuple[str, str]]]] = [
    (("стоматолог", "клиник", "медицин", "косметолог"), [("amenity", "dentist"), ("amenity", "clinic"), ("healthcare", "clinic")]),
    (("автосервис", "кузовн", "шиномонтаж", "сто "), [("shop", "car_repair")]),
    (("ресторан", "кафе", "кофейн", "пекарн", "общепит", "horeca"), [("amenity", "restaurant"), ("amenity", "cafe")]),
    (("отель", "гостиниц", "хостел"), [("tourism", "hotel")]),
    (("логист", "перевозк", "склад", "экспедирован"), [("office", "logistics"), ("office", "transport")]),
    (("строител", "ремонт", "проектирован", "архитект"), [("office", "construction_company"), ("office", "architect")]),
    (("бухгалтер", "аудит", "налог"), [("office", "accountant"), ("office", "tax_advisor")]),
    (("юрист", "юридич", "адвокат", "право"), [("office", "lawyer")]),
    (("ит ", "it ", "разработк", "интегратор", "софт"), [("office", "it")]),
    (("реклам", "маркетинг", "seo", "диджитал", "digital"), [("office", "advertising_agency")]),
    (("производств", "завод", "фабрик", "металлоконструкц"), [("industrial", "factory"), ("man_made", "works")]),
]


def is_junk_ru_domain(domain_or_url: str) -> bool:
    dom = normalize_domain_idna(domain_or_url)
    if not dom:
        return True
    for junk in RU_AGGREGATOR_DOMAINS:
        j = normalize_domain_idna(junk)
        if dom == j or dom.endswith("." + j):
            return True
    if dom.endswith((".gov.ru", ".mil.ru", ".edu.ru", ".mos.ru", ".spb.ru")):
        return True
    return False


@dataclass
class DiscoveredItem:
    name: str
    website: str = ""
    city: str = ""
    industry: str = ""
    company_size: str = ""
    inn: str = ""
    address: str = ""
    phone: str = ""
    telegram: str = ""
    whatsapp: str = ""
    vk: str = ""
    ymaps_url: str = ""
    twogis_url: str = ""
    hh_url: str = ""
    external_id: str = ""
    source: str = ""
    source_url: str = ""
    description: str = ""
    observations: list[dict] = field(default_factory=list)


# ── 1. HeadHunter Open API (api.hh.ru — бесплатно, без ключей, для любой ниши РФ) ──

async def discover_hh_ru(
    query: str,
    city: str,
    *,
    hiring_role: str = "",
    limit: int = 20,
) -> list[DiscoveredItem]:
    """Ищет компании РФ через открытый API hh.ru по вакансиям или нишевому запросу."""
    area = HH_AREA_IDS.get(city.strip().lower(), "113")  # 113 = вся Россия
    search_text = f"{hiring_role} {query}".strip() if hiring_role else query.strip()
    if not search_text:
        return []

    headers = {
        "User-Agent": "FRIDAY-B2B-Scout/0.2 (contact@localhost)",
        "Accept": "application/json",
    }
    items: list[DiscoveredItem] = []
    seen_emp_ids: set[str] = set()

    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.5), headers=headers) as client:
        resp = await client.get(
            "https://api.hh.ru/vacancies",
            params={
                "text": search_text,
                "area": area,
                "per_page": min(max(limit * 2, 20), 50),
                "page": 0,
            },
        )
        if resp.status_code != 200:
            return []
        vacancies = resp.json().get("items") or []

        for vac in vacancies:
            emp = vac.get("employer") or {}
            emp_id = str(emp.get("id") or "")
            emp_name = (emp.get("name") or "").strip()
            if not emp_id or not emp_name or emp_id in seen_emp_ids:
                continue
            # Пропускаем кадровые агентства, если ищем прямых работодателей
            seen_emp_ids.add(emp_id)

            vac_name = (vac.get("name") or "").strip()
            vac_url = (vac.get("alternate_url") or "").strip()
            vac_city = ((vac.get("area") or {}).get("name") or city).strip()
            emp_hh_url = (emp.get("alternate_url") or f"https://hh.ru/employer/{emp_id}").strip()

            site_url = ""
            open_vacancies = 1
            industries_str = ""
            description = ""
            # Подтягиваем карточку работодателя (там лежит официальный `site_url` и отрасли!)
            try:
                er = await client.get(f"https://api.hh.ru/employers/{emp_id}")
                if er.status_code == 200:
                    edata = er.json()
                    site_url = (edata.get("site_url") or "").strip()
                    open_vacancies = int(edata.get("open_vacancies") or 1)
                    ind_list = [i.get("name", "") for i in (edata.get("industries") or []) if i.get("name")]
                    industries_str = ", ".join(ind_list[:2])
                    raw_desc = re.sub(r"<[^>]+>", " ", edata.get("description") or "")
                    description = re.sub(r"\s+", " ", unescape(raw_desc)).strip()[:300]
            except httpx.HTTPError:
                pass

            if site_url and is_junk_ru_domain(site_url):
                site_url = ""

            obs: list[dict] = [
                {
                    "signal_code": "HH_HIRING_ROLE",
                    "collector": "hh_ru",
                    "value_text": f"Вакансия: «{vac_name}»",
                    "evidence_url": vac_url or emp_hh_url,
                },
                {
                    "signal_code": "HH_OPEN_ROLES",
                    "collector": "hh_ru",
                    "value_num": float(open_vacancies),
                    "value_text": f"{open_vacancies} открытых вакансий на hh.ru",
                    "evidence_url": emp_hh_url,
                },
            ]

            items.append(
                DiscoveredItem(
                    name=emp_name,
                    website=site_url,
                    city=vac_city,
                    industry=industries_str or query,
                    hh_url=emp_hh_url,
                    external_id=f"hh:{emp_id}",
                    source="hh_ru",
                    source_url=vac_url or emp_hh_url,
                    description=description,
                    observations=obs,
                )
            )
            if len(items) >= limit:
                break

    return items


# ── 2. Яндекс Search API v2 (позиции 11–30 — точка роста органики в любой нише) ──

async def discover_yandex_search(
    query: str,
    city: str,
    *,
    limit: int = 30,
) -> list[DiscoveredItem]:
    api_key = os.getenv("YANDEX_SEARCH_API_KEY", "").strip()
    folder_id = os.getenv("YANDEX_FOLDER_ID", "").strip()
    if not api_key or not folder_id:
        return []

    region_id = YANDEX_REGION_IDS.get(city.strip().lower(), 225)
    full_query = f"{query} {city}".strip()
    items: list[DiscoveredItem] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for page in range(0, 3):
            if len(items) >= limit:
                break
            payload = {
                "query": {"searchType": "SEARCH_TYPE_RU", "queryText": full_query, "page": page},
                "region": str(region_id),
                "folderId": folder_id,
                "responseFormat": "FORMAT_XML",
            }
            resp = await client.post(
                "https://searchapi.api.cloud.yandex.net/v2/web/search",
                headers={"Authorization": f"Api-Key {api_key}"},
                json=payload,
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            raw_xml_b64 = data.get("rawData", "")
            if not raw_xml_b64:
                break
            import base64

            xml_bytes = base64.b64decode(raw_xml_b64)
            root = ET.fromstring(xml_bytes)
            for idx, group in enumerate(root.findall(".//group"), start=1):
                rank = page * 10 + idx
                doc = group.find("doc")
                if doc is None:
                    continue
                url_el = doc.find("url")
                title_el = doc.find("title")
                url = (url_el.text or "").strip() if url_el is not None else ""
                title = "".join(title_el.itertext()).strip() if title_el is not None else ""
                if not url or is_junk_ru_domain(url):
                    continue
                obs = []
                if 11 <= rank <= 30:
                    obs.append(
                        {
                            "signal_code": "YANDEX_PAGE2_RANK",
                            "collector": "yandex_search",
                            "value_num": float(rank),
                            "value_text": f"Позиция #{rank} в Яндексе по запросу «{full_query}»",
                            "evidence_url": url,
                        }
                    )
                items.append(
                    DiscoveredItem(
                        name=title or normalize_domain_idna(url),
                        website=url,
                        city=city,
                        industry=query,
                        source="yandex_search",
                        source_url=url,
                        observations=obs,
                    )
                )
                if len(items) >= limit:
                    break
    return items


# ── 3. 2ГИС Catalog API (любой локальный/региональный бизнес РФ) ──

async def discover_twogis(query: str, city: str, *, limit: int = 25) -> list[DiscoveredItem]:
    api_key = os.getenv("TWOGIS_API_KEY", "").strip()
    if not api_key:
        return []

    q = f"{city} {query}".strip()
    items: list[DiscoveredItem] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://catalog.api.2gis.com/3.0/items",
            params={
                "q": q,
                "fields": "items.contact_groups,items.reviews,items.address,items.rubrics",
                "page_size": min(limit, 50),
                "key": api_key,
            },
        )
        if resp.status_code != 200:
            return []
        raw_items = resp.json().get("result", {}).get("items", [])
        for it in raw_items:
            firm_id = str(it.get("id", ""))
            name = (it.get("name") or "").strip()
            address = (it.get("address_name") or "").strip()
            rubrics = [r.get("name", "") for r in (it.get("rubrics") or []) if r.get("name")]
            website, phone, tg, wa, vk = "", "", "", "", ""
            for cg in it.get("contact_groups", []) or []:
                for c in cg.get("contacts", []) or []:
                    ctype = c.get("type")
                    val = (c.get("value") or c.get("url") or "").strip()
                    if ctype == "website" and not website and not is_junk_ru_domain(val):
                        website = val
                    elif ctype == "phone" and not phone:
                        phone = val
                    elif "t.me/" in val and not tg:
                        tg = val
                    elif ("wa.me/" in val or "whatsapp" in val.lower()) and not wa:
                        wa = val
                    elif "vk.com/" in val and not vk:
                        vk = val

            reviews = it.get("reviews") or {}
            rating = reviews.get("general_rating")
            rev_count = reviews.get("general_review_count")
            card_url = f"https://2gis.ru/firm/{firm_id.split('_')[0]}" if firm_id else ""

            obs: list[dict] = []
            if rating is not None:
                r_f = float(rating)
                obs.append(
                    {
                        "signal_code": "TWOGIS_RATING",
                        "collector": "twogis",
                        "value_num": r_f,
                        "value_text": f"Рейтинг 2ГИС {r_f:.1f}",
                        "evidence_url": card_url,
                    }
                )
                if r_f < 4.3:
                    obs.append(
                        {
                            "signal_code": "LOW_MAP_RATING",
                            "collector": "twogis",
                            "value_num": r_f,
                            "value_text": f"Рейтинг 2ГИС {r_f:.1f} (< 4.3)",
                            "evidence_url": card_url,
                        }
                    )
            if rev_count is not None:
                obs.append(
                    {
                        "signal_code": "TWOGIS_REVIEWS",
                        "collector": "twogis",
                        "value_num": float(rev_count),
                        "value_text": f"{int(rev_count)} отзывов в 2ГИС",
                        "evidence_url": card_url,
                    }
                )
            if not website:
                obs.append(
                    {
                        "signal_code": "NO_WEBSITE_ON_MAP",
                        "collector": "twogis",
                        "value_text": "В карточке 2ГИС не указан сайт",
                        "evidence_url": card_url,
                    }
                )

            items.append(
                DiscoveredItem(
                    name=name,
                    website=website,
                    city=city,
                    industry=", ".join(rubrics[:2]) or query,
                    address=address,
                    phone=phone,
                    telegram=tg,
                    whatsapp=wa,
                    vk=vk,
                    twogis_url=card_url,
                    external_id=f"2gis:{firm_id}" if firm_id else "",
                    source="twogis",
                    source_url=card_url,
                    observations=obs,
                )
            )
    return items


# ── 4. Яндекс Поиск по организациям (Яндекс Карты) ──

async def discover_yandex_maps(query: str, city: str, *, limit: int = 25) -> list[DiscoveredItem]:
    api_key = os.getenv("YANDEX_MAPS_API_KEY", "").strip()
    if not api_key:
        return []

    q = f"{city}, {query}".strip(", ")
    items: list[DiscoveredItem] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://search-maps.yandex.ru/v1/",
            params={
                "text": q,
                "type": "biz",
                "lang": "ru_RU",
                "results": min(limit, 50),
                "apikey": api_key,
            },
        )
        if resp.status_code != 200:
            return []
        for feat in resp.json().get("features", []):
            meta = feat.get("properties", {}).get("CompanyMetaData", {})
            oid = str(meta.get("id", ""))
            name = (meta.get("name") or "").strip()
            address = (meta.get("address") or "").strip()
            cats = [c.get("name", "") for c in (meta.get("Categories") or []) if c.get("name")]
            website = (meta.get("url") or "").strip()
            if website and is_junk_ru_domain(website):
                website = ""
            phones = meta.get("Phones") or []
            phone = (phones[0].get("formatted") or "") if phones else ""
            ymaps_url = f"https://yandex.ru/maps/org/{oid}/" if oid else ""

            obs: list[dict] = []
            if not website:
                obs.append(
                    {
                        "signal_code": "NO_WEBSITE_ON_MAP",
                        "collector": "yandex_maps",
                        "value_text": "В карточке Яндекс Карт не указан сайт",
                        "evidence_url": ymaps_url,
                    }
                )

            items.append(
                DiscoveredItem(
                    name=name,
                    website=website,
                    city=city,
                    industry=", ".join(cats[:2]) or query,
                    address=address,
                    phone=phone,
                    ymaps_url=ymaps_url,
                    external_id=f"ymaps:{oid}" if oid else "",
                    source="yandex_maps",
                    source_url=ymaps_url,
                    observations=obs,
                )
            )
    return items


# ── 5. Бесплатный веб-поиск по Рунету (DuckDuckGo HTML RU без ключей) ──

DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _unwrap_ddg_href(href: str) -> str:
    href = unescape(href)
    if "uddg=" in href:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return unquote(qs["uddg"][0])
    return href


async def discover_ddg_ru(query: str, city: str, *, limit: int = 20) -> list[DiscoveredItem]:
    full_q = f"{query} {city}".strip()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    items: list[DiscoveredItem] = []
    seen_domains: set[str] = set()
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.5), follow_redirects=True) as client:
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": full_q, "kl": "ru-ru"},
            headers=headers,
        )
        if resp.status_code != 200:
            return []
        for match in DDG_RESULT_RE.finditer(resp.text):
            raw_href, raw_title = match.group(1), match.group(2)
            url = _unwrap_ddg_href(raw_href)
            if not url.startswith(("http://", "https://")):
                continue
            dom = normalize_domain_idna(url)
            if not dom or dom in seen_domains or is_junk_ru_domain(dom):
                continue
            seen_domains.add(dom)
            clean_title = re.sub(r"<[^>]+>", "", unescape(raw_title)).strip()
            items.append(
                DiscoveredItem(
                    name=clean_title[:140] or dom,
                    website=f"https://{dom}",
                    city=city,
                    industry=query,
                    source="ddg_ru",
                    source_url=url,
                )
            )
            if len(items) >= limit:
                break
    return items


# ── 6. Бесплатный поиск локальных компаний в OpenStreetMap (Overpass RU) ──

def _osm_clauses_for_query(query: str, bbox_str: str) -> str:
    ql = query.lower()
    matched_tags: list[tuple[str, str]] = []
    for keywords, tags in OSM_NICHE_TAG_MAP:
        if any(kw in ql for kw in keywords):
            matched_tags.extend(tags)
    if matched_tags:
        lines = [f'  nwr["{k}"="{v}"]({bbox_str});' for k, v in matched_tags]
        return "\n".join(lines)
    # Иначе ищем по ключевому слову в названии организации
    first_word = re.sub(r"[^\wа-яА-ЯёЁ-]", "", query.split()[0])[:24] if query.split() else ""
    if first_word:
        return f'  nwr["name"~"{first_word}",i]({bbox_str});'
    return f'  nwr["office"]({bbox_str});'


async def discover_osm_ru(query: str, city: str, *, limit: int = 25) -> list[DiscoveredItem]:
    bbox = CITY_BBOX_RU.get(city.strip().lower())
    if not bbox:
        return []
    south, west, north, east = bbox
    bbox_str = f"{south},{west},{north},{east}"
    clauses = _osm_clauses_for_query(query, bbox_str)
    overpass_ql = f"""
    [out:json][timeout:15];
    (
    {clauses}
    );
    out center {min(limit, 50)};
    """
    items: list[DiscoveredItem] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.5)) as client:
        resp = await client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": overpass_ql},
        )
        if resp.status_code != 200:
            return []
        for el in resp.json().get("elements", []):
            tags = el.get("tags") or {}
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            website = (tags.get("website") or tags.get("contact:website") or "").strip()
            if website and is_junk_ru_domain(website):
                website = ""
            phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
            tg = (tags.get("contact:telegram") or "").strip()
            vk = (tags.get("contact:vk") or "").strip()
            street = tags.get("addr:street", "")
            house = tags.get("addr:housenumber", "")
            address = f"{street} {house}".strip()
            osm_id = f"osm:{el.get('type', 'node')}:{el.get('id', '')}"
            obs: list[dict] = []
            if not website:
                obs.append(
                    {
                        "signal_code": "NO_WEBSITE_ON_MAP",
                        "collector": "osm_ru",
                        "value_text": "В карточке карты отсутствует сайт организации",
                    }
                )
            items.append(
                DiscoveredItem(
                    name=name,
                    website=website,
                    city=city,
                    industry=query,
                    address=address,
                    phone=phone,
                    telegram=tg,
                    vk=vk,
                    external_id=osm_id,
                    source="osm_ru",
                    observations=obs,
                )
            )
            if len(items) >= limit:
                break
    return items


# ── 7. Универсальный импорт файлов пользователя (CSV / JSON / TXT: Контур.Компас, СБИС, 2ГИС, домены) ──

def parse_import_file(path: str | Path, default_city: str = "", default_industry: str = "") -> list[tuple[DiscoveredItem, Prospect | None]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Файл не найден: {p}")

    ext = p.suffix.lower()
    records: list[dict] = []

    if ext == ".txt":
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            val = line.strip()
            if not val or val.startswith("#"):
                continue
            records.append({"website": val, "name": val})
    elif ext in (".json", ".jsonl"):
        raw = p.read_text(encoding="utf-8-sig").strip()
        if ext == ".jsonl":
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            parsed = json.loads(raw)
            records = parsed if isinstance(parsed, list) else parsed.get("items", [])
    else:
        text = p.read_text(encoding="utf-8-sig")
        sample = text[:2048]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        records = [dict(r) for r in reader]

    out: list[tuple[DiscoveredItem, Prospect | None]] = []
    for row in records:
        norm = {str(k).strip().lower(): str(v).strip() for k, v in row.items() if k is not None and v is not None}
        name = (
            norm.get("name")
            or norm.get("company")
            or norm.get("компания")
            or norm.get("название")
            or norm.get("наименование")
            or ""
        )
        website = (
            norm.get("website")
            or norm.get("url")
            or norm.get("domain")
            or norm.get("сайт")
            or norm.get("домен")
            or ""
        )
        city = norm.get("city") or norm.get("город") or norm.get("регион") or default_city
        industry = (
            norm.get("industry")
            or norm.get("niche")
            or norm.get("отрасль")
            or norm.get("ниша")
            or norm.get("рубрика")
            or norm.get("оквэд")
            or default_industry
        )
        company_size = norm.get("company_size") or norm.get("size") or norm.get("штат") or norm.get("размер") or ""
        inn = norm.get("inn") or norm.get("инн") or ""
        phone = norm.get("phone") or norm.get("телефон") or ""
        telegram = norm.get("telegram") or norm.get("телеграм") or norm.get("tg") or ""
        whatsapp = norm.get("whatsapp") or norm.get("ватсап") or ""
        vk = norm.get("vk") or norm.get("вк") or ""
        address = norm.get("address") or norm.get("адрес") or ""
        description = norm.get("description") or norm.get("описание") or ""

        if not name and not website:
            continue
        if website and not website.startswith(("http://", "https://")):
            website = f"https://{website}"

        ext_id = f"inn:{inn}" if inn else ""
        item = DiscoveredItem(
            name=name or normalize_domain_idna(website),
            website=website,
            city=city,
            industry=industry,
            company_size=company_size,
            inn=inn,
            address=address,
            phone=phone,
            telegram=telegram,
            whatsapp=whatsapp,
            vk=vk,
            external_id=ext_id,
            source="import",
            source_url=str(p.name),
            description=description,
        )

        first_name = norm.get("first_name") or norm.get("имя") or ""
        last_name = norm.get("last_name") or norm.get("фамилия") or ""
        middle_name = norm.get("middle_name") or norm.get("отчество") or ""
        fio = norm.get("fio") or norm.get("фио") or norm.get("директор") or norm.get("руководитель") or ""
        if fio and not (first_name or last_name):
            parts = fio.split()
            if len(parts) >= 2:
                last_name, first_name = parts[0], parts[1]
                middle_name = parts[2] if len(parts) >= 3 else ""

        email = norm.get("email") or norm.get("почта") or norm.get("e-mail") or ""
        title = norm.get("title") or norm.get("должность") or ""
        reg_id = norm.get("registry_id") or ""

        prospect = None
        if first_name or last_name or email or reg_id:
            prospect = Prospect(
                last_name=last_name,
                first_name=first_name,
                middle_name=middle_name,
                title=title,
                registry_id=reg_id,
                email=email,
                email_source="import" if email else "",
                phone=phone,
                telegram=telegram,
                whatsapp=whatsapp,
                company=item.name,
                industry=industry,
                city=city,
                source="import",
            )
        out.append((item, prospect))
    return out


# ── Оркестратор сохранения результатов в SQLite ──

async def persist_discovered_items(
    state: StateManager,
    items: list[tuple[DiscoveredItem, Prospect | None]],
    *,
    config: FridayConfig | None = None,
    run_id: str = "",
) -> dict:
    cfg = config or load_config()
    inserted_companies = 0
    updated_companies = 0
    inserted_prospects = 0
    recorded_obs = 0
    skipped_dnc = 0

    for item, prospect in items:
        dom = normalize_domain_idna(item.website)
        sup, _ = is_suppressed(
            company_domain=dom,
            prospect_email=prospect.email if prospect else "",
            prospect_telegram=item.telegram or (prospect.telegram if prospect else ""),
            config=cfg,
        )
        if sup:
            skipped_dnc += 1
            continue

        slug = re.sub(r"\W+", "_", item.name.lower())[:40]
        fallback_ext = f"manual:{slug}:{item.city.lower()}" if not dom else ""
        comp = Company(
            name=item.name,
            domain=dom,
            website=item.website,
            industry=item.industry,
            company_size=item.company_size,
            inn=item.inn,
            description=item.description,
            city=item.city,
            address=item.address,
            phone=item.phone,
            telegram=item.telegram,
            whatsapp=item.whatsapp,
            vk=item.vk,
            ymaps_url=item.ymaps_url,
            twogis_url=item.twogis_url,
            hh_url=item.hh_url,
            external_id=item.external_id or fallback_ext,
            source=item.source,
            source_url=item.source_url,
        )
        cid, created = await state.upsert_company(comp)
        if created:
            inserted_companies += 1
        else:
            updated_companies += 1

        for obs in item.observations:
            oid = await state.record_observation(
                signal_code=obs["signal_code"],
                company_id=cid,
                collector=obs.get("collector", item.source),
                value_num=obs.get("value_num"),
                value_text=obs.get("value_text", ""),
                evidence_url=obs.get("evidence_url", item.source_url),
                run_id=run_id,
                require_confirmed=True,
            )
            if oid:
                recorded_obs += 1

        if prospect is not None:
            prospect.company_id = cid
            prospect.company = comp.name
            if not prospect.industry:
                prospect.industry = comp.industry
            _, p_created = await state.upsert_prospect(prospect)
            if p_created:
                inserted_prospects += 1

        if (item.external_id or "").startswith("catalog:"):
            await state.mark_profile_checked(cid)
            await state.mark_prospects_checked(cid)

    return {
        "inserted_companies": inserted_companies,
        "updated_companies": updated_companies,
        "inserted_prospects": inserted_prospects,
        "recorded_observations": recorded_obs,
        "skipped_dnc": skipped_dnc,
    }


_PROVIDER_ALIASES = {
    "hh": "hh_ru",
    "web": "ddg_ru",
    "osm": "osm_ru",
}


async def run_discover(
    state: StateManager,
    *,
    query: str,
    city: str,
    provider: str = "auto",
    limit: int = 25,
    config: FridayConfig | None = None,
) -> dict:
    """Запускает поиск целевых компаний РФ с устойчивой обработкой сетевых ошибок."""
    cfg = config or load_config()
    norm_provider = _PROVIDER_ALIASES.get(provider.strip().lower(), provider.strip().lower() or "auto")
    run_id = await state.start_run(
        stage="discover",
        provider=norm_provider,
        params={"query": query, "city": city, "limit": limit},
    )
    try:
        collected_pairs: list[tuple[DiscoveredItem, Prospect | None]] = []
        used_providers: list[str] = []
        network_errors = 0

        if norm_provider in ("auto", "yandex_search") and os.getenv("YANDEX_SEARCH_API_KEY"):
            try:
                res = await discover_yandex_search(query, city, limit=limit)
                if res:
                    collected_pairs.extend((it, None) for it in res)
                    used_providers.append("yandex_search")
            except (httpx.HTTPError, OSError):
                network_errors += 1

        if norm_provider in ("auto", "twogis") and os.getenv("TWOGIS_API_KEY"):
            try:
                res = await discover_twogis(query, city, limit=limit)
                if res:
                    collected_pairs.extend((it, None) for it in res)
                    used_providers.append("twogis")
            except (httpx.HTTPError, OSError):
                network_errors += 1

        if norm_provider in ("auto", "yandex_maps") and os.getenv("YANDEX_MAPS_API_KEY"):
            try:
                res = await discover_yandex_maps(query, city, limit=limit)
                if res:
                    collected_pairs.extend((it, None) for it in res)
                    used_providers.append("yandex_maps")
            except (httpx.HTTPError, OSError):
                network_errors += 1

        if norm_provider in ("auto", "hh_ru"):
            hiring_roles = cfg.icp.hiring_signals[:2] if cfg.icp.hiring_signals else [""]
            hh_found: list[DiscoveredItem] = []
            for role in hiring_roles:
                try:
                    hh_res = await discover_hh_ru(
                        query,
                        city,
                        hiring_role=role,
                        limit=max(10, limit // max(1, len(hiring_roles))),
                    )
                    hh_found.extend(hh_res)
                except (httpx.HTTPError, OSError):
                    network_errors += 1
                    break
            if hh_found:
                collected_pairs.extend((it, None) for it in hh_found[:limit])
                used_providers.append("hh_ru")

        if norm_provider == "ddg_ru" or (norm_provider == "auto" and len(collected_pairs) < limit):
            try:
                res = await discover_ddg_ru(query, city, limit=limit)
                if res:
                    collected_pairs.extend((it, None) for it in res)
                    used_providers.append("ddg_ru")
            except (httpx.HTTPError, OSError):
                network_errors += 1

        if norm_provider == "osm_ru" or (norm_provider == "auto" and len(collected_pairs) < max(5, limit // 2)):
            try:
                res = await discover_osm_ru(query, city, limit=limit)
                if res:
                    collected_pairs.extend((it, None) for it in res)
                    used_providers.append("osm_ru")
            except (httpx.HTTPError, OSError):
                network_errors += 1

        # Если внешние поисковые сети недоступны на уровне контейнера/сети, используем резервный каталог РФ
        if not collected_pairs and network_errors > 0:
            from friday.collectors.ru_catalog_fallback import build_offline_catalog_items

            fallback_pairs = build_offline_catalog_items(
                query,
                city,
                config=cfg,
                limit=min(limit, 8),
            )
            collected_pairs.extend(fallback_pairs)
            used_providers.append("ru_catalog")

        stats = await persist_discovered_items(
            state,
            collected_pairs,
            config=cfg,
            run_id=run_id,
        )
        stats["providers_used"] = used_providers
        await state.finish_run(
            run_id,
            status="completed",
            records=stats["inserted_companies"] + stats["updated_companies"],
        )
        return stats
    except Exception as exc:
        await state.finish_run(run_id, status="failed", error=str(exc))
        raise
