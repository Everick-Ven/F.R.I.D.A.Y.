"""DISCOVER — поиск адвокатских образований, практик и реестровых записей в РФ.

Поддерживает все 4 целевых контура источников:
1. Поиск Яндекса по городу и теме (`yandex_search` через Yandex Search API / XML + бесплатный `duckduckgo_ru`).
2. Яндекс Карты и 2ГИС (`yandex_maps`, `twogis`, плюс бесплатный `osm_ru` без ключей).
3. Реестры адвокатов регионов / Минюста РФ (`import_registry_file` — CSV/JSON/HTML таблицы с фильтрацией действующего статуса).
4. Собственный список пользователя (`import_custom_list` — CSV/TSV/JSON/TXT со свободными русскими заголовками).
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from friday.config import EnvConfig, FridayConfig
from friday.gate import display_domain, match_do_not_contact, normalize_domain_idna
from friday.models import Company, Prospect
from friday.state import StateManager
from friday.textio import read_user_text

logger = logging.getLogger("friday.collectors.discover")

TIMEOUT = httpx.Timeout(connect=10.0, read=35.0, write=10.0, pool=10.0)

# Координаты центров крупнейших городов РФ (широта, долгота) — мгновенный геокодинг без лишних запросов
RU_CITY_COORDS: dict[str, tuple[float, float]] = {
    "москва": (55.7558, 37.6173),
    "санкт-петербург": (59.9343, 30.3351),
    "петербург": (59.9343, 30.3351),
    "спб": (59.9343, 30.3351),
    "екатеринбург": (56.8389, 60.6057),
    "новосибирск": (55.0084, 82.9357),
    "казань": (55.7961, 49.1064),
    "нижний новгород": (56.3269, 44.0059),
    "краснодар": (45.0355, 38.9753),
    "самара": (53.1959, 50.1002),
    "ростов-на-дону": (47.2357, 39.7015),
    "уфа": (54.7388, 55.9721),
    "красноярск": (56.0153, 92.8932),
    "воронеж": (51.6608, 39.2003),
    "пермь": (58.0105, 56.2502),
    "волгоград": (48.7080, 44.5133),
    "челябинск": (55.1644, 61.4368),
    "тюмень": (57.1522, 65.5272),
    "владивосток": (43.1155, 131.8855),
    "сочи": (43.5855, 39.7231),
    "калининград": (54.7104, 20.4522),
    "иркутск": (52.2870, 104.3050),
    "хабаровск": (48.4802, 135.0719),
    "саратов": (51.5336, 46.0343),
    "омск": (54.9885, 73.3242),
}

# Коды регионов поиска Яндекса (lr) для точной гео-выдачи
YANDEX_REGION_CODES: dict[str, int] = {
    "москва": 213,
    "санкт-петербург": 2,
    "петербург": 2,
    "спб": 2,
    "екатеринбург": 54,
    "новосибирск": 65,
    "казань": 43,
    "нижний новгород": 47,
    "краснодар": 35,
    "самара": 51,
    "ростов-на-дону": 39,
    "уфа": 172,
    "красноярск": 62,
    "воронеж": 193,
    "пермь": 50,
    "волгоград": 38,
    "челябинск": 56,
    "тюмень": 55,
    "владивосток": 75,
    "калининград": 22,
}

# Паттерны доменов-агрегаторов, справочников, судов и госструктур РФ (никогда не являются лидами)
JUNK_DOMAIN_PATTERNS_RU: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.I)
    for p in (
        # Российские юридические агрегаторы и маркетплейсы услуг
        r"(^|\.)(pravoved|9111|harant|pravo|zakon|sudact|consultant|garant|kodeks|dogovor-urist)\.",
        r"(^|\.)(zoon|profi|avito|youdo|yell|flamp|spravker|orgpage|cataloxy|blizko|pulscen)\.",
        r"(^|\.)(otzovik|irecommend|banki|sravni|cian|domclick)\.",
        # Поисковики, карты, соцсети, медиа
        r"(^|\.)(yandex|ya|2gis|google|bing|duckduckgo|mail|rambler)\.",
        r"(^|\.)(vk|ok|dzen|vc|habr|rbc|kommersant|vedomosti|forbes|rg|tass|ria|lenta)\.",
        r"(^|\.)(telegram|t\.me|whatsapp|wa\.me|youtube|rutube|wikipedia|tenchat)\.",
        # Проверка контрагентов и работные сайты
        r"(^|\.)(rusprofile|spark-interfax|sbis|list-org|checko|zachestnyibiznes|audit-it)\.",
        r"(^|\.)(hh|superjob|rabota|zarplata|trudvsem)\.",
        # Госорганы, суды, нотариат, адвокатские палаты (органы самоуправления)
        r"\.(gov|mil|edu)\.ru$",
        r"(^|\.)(sudrf|arbitr|vsrf|ksrf|gosuslugi|nalog|fssp|minjust|notariat|fparf)\.",
        r"(^|\.)(advokatymoscow|apspb|apmo)\.",
        # Конструкторы на служебных поддоменах без своего домена — кроме персональных визиток Tilda при явном импорте
        r"(^|\.)(wordpress|wix|blogspot|narod)\.com$",
    )
)

# Названия подборок и рейтингов в поисковой выдаче (не конкретное адвокатское образование)
JUNK_NAME_PATTERNS_RU: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"^\s*(?:топ|лучшие|рейтинг|список|каталог|все)\s*[-–—]?\s*\d*\s*(?:лучших\s+)?(?:адвокат|юрист|коллеги|бюро)",
        r"\b(?:отзывы\s+о\s+юристах|найти\s+адвоката|каталог\s+адвокатов|реестр\s+адвокатов)\b",
        r"\b(?:районный\s+суд|арбитражный\s+суд|мировой\s+судья|прокуратура|следственный\s+комитет|нотариус)\b",
    )
)

REGISTRY_NUMBER_RE = re.compile(r"\b(\d{2}/\d{1,6})\b")


def detect_legal_form(text: str) -> str:
    """Определить форму адвокатского образования / практики по названию или описанию."""
    low = (text or "").lower().replace("ё", "е")
    if "коллегия адвокатов" in low or re.search(r"\b(?:мка|мока|спбка|ка)\b", low):
        return "Коллегия адвокатов"
    if "адвокатское бюро" in low or re.search(r"\bаб\s+[«\"']", low):
        return "Адвокатское бюро"
    if "адвокатский кабинет" in low:
        return "Адвокатский кабинет"
    if "юридическая консультация" in low:
        return "Юридическая консультация"
    if "адвокат" in low:
        return "Адвокатская практика"
    if any(w in low for w in ("юридическ", "правовой центр", "юрфирма", "партнеры")):
        return "Юридическая фирма"
    return ""


def clean_practice_name(raw_title: str, domain: str = "") -> str:
    """Очистить SEO-заголовок выдачи до аккуратного названия практики."""
    title = unescape(re.sub(r"\s+", " ", (raw_title or "")).strip())
    if not title:
        return display_domain(domain)
    parts = [p.strip(" ,.-") for p in re.split(r"\s+[|\-–—•·]\s+|\s*[|•·]\s*", title) if p.strip()]
    for seg in parts:
        if any(k in seg.lower() for k in ("коллегия", "бюро", "кабинет", "адвокат", "партнер", "партнёр")):
            if not any(p.search(seg) for p in JUNK_NAME_PATTERNS_RU) and len(seg) <= 85:
                return seg
    best = parts[0] if parts else title
    return best[:85]


def is_junk_ru(domain: str, name: str = "") -> bool:
    """Проверить, является ли запись агрегатором/мусором, который не нужно сохранять в базу."""
    norm = normalize_domain_idna(domain)
    disp = display_domain(domain)
    for host in {norm, disp}:
        if host and any(p.search(host) for p in JUNK_DOMAIN_PATTERNS_RU):
            return True
    if name and any(p.search(name) for p in JUNK_NAME_PATTERNS_RU):
        return True
    return False


@dataclass
class DiscoveredPractice:
    """Нормализованная карточка найденной юридической практики / адвоката."""

    name: str = ""
    domain: str = ""
    website: str = ""
    legal_form: str = ""
    practice_area: str = ""
    city: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    telegram: str = ""
    whatsapp: str = ""
    vk: str = ""
    ymaps_url: str = ""
    twogis_url: str = ""
    rating: float | None = None
    review_count: int | None = None
    has_website: bool | None = None
    card_incomplete: bool = False
    yandex_rank: int | None = None
    keyword: str = ""
    external_id: str = ""
    source: str = ""
    source_url: str = ""
    # Для записей из реестра или именных карточек:
    advocate_fio: str = ""
    registry_number: str = ""
    registry_status: str = ""


@dataclass
class DiscoveryQuery:
    term: str
    city: str = ""
    depth: int = 30
    limit: int = 50

    def keyword(self) -> str:
        return f"{self.term} {self.city}".strip()


@dataclass
class DiscoveryReport:
    provider: str = ""
    queries_run: int = 0
    found_raw: int = 0
    new_companies: int = 0
    updated_companies: int = 0
    new_prospects: int = 0
    observations_written: int = 0
    estimated_cost_rub: float = 0.0
    actual_cost_rub: float = 0.0
    skipped_junk: int = 0
    skipped_dnc: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "queries_run": self.queries_run,
            "found_raw": self.found_raw,
            "new_companies": self.new_companies,
            "updated_companies": self.updated_companies,
            "new_prospects": self.new_prospects,
            "observations_written": self.observations_written,
            "estimated_cost_rub": round(self.estimated_cost_rub, 2),
            "actual_cost_rub": round(self.actual_cost_rub, 2),
            "skipped_junk": self.skipped_junk,
            "skipped_dnc": self.skipped_dnc,
            "errors": self.errors,
        }


class DiscoveryProvider:
    key: str = ""
    label: str = ""
    kind: str = "listings"   # serp | listings
    blurb: str = ""
    cost_per_query_rub: float = 0.0
    env_keys: tuple[str, ...] = ()

    def missing_keys(self, env: EnvConfig) -> list[str]:
        d = env.as_dict()
        return [k for k in self.env_keys if not (d.get(k) or "").strip()]

    def configured(self, env: EnvConfig) -> bool:
        return len(self.missing_keys(env)) == 0

    def estimate_rub(self, queries: list[DiscoveryQuery]) -> float:
        return round(len(queries) * self.cost_per_query_rub, 2)

    async def fetch(
        self, client: httpx.AsyncClient, env: EnvConfig, query: DiscoveryQuery
    ) -> list[DiscoveredPractice]:
        raise NotImplementedError


class YandexSearchProvider(DiscoveryProvider):
    """Органическая выдача Яндекса (Yandex Cloud Search API v2 или Yandex XML прокси)."""

    key = "yandex_search"
    label = "Поиск Яндекса (SERP 1–30)"
    kind = "serp"
    blurb = (
        "Находит сайты адвокатов и коллегий по запросу «<практика> <город>» и фиксирует "
        "точную позицию в Яндексе (позиции 11–30 — лучший сигнал для аутрича)."
    )
    cost_per_query_rub = 0.45

    def missing_keys(self, env: EnvConfig) -> list[str]:
        if env.yandex_xml_url:
            return []
        missing = []
        if not env.yandex_search_api_key:
            missing.append("YANDEX_SEARCH_API_KEY")
        if not env.yandex_search_folder_id:
            missing.append("YANDEX_SEARCH_FOLDER_ID")
        return missing

    async def fetch(
        self, client: httpx.AsyncClient, env: EnvConfig, query: DiscoveryQuery
    ) -> list[DiscoveredPractice]:
        lr = YANDEX_REGION_CODES.get(query.city.strip().lower(), 225)  # 225 = вся Россия
        if env.yandex_xml_url:
            resp = await client.get(
                env.yandex_xml_url,
                params={"query": query.keyword(), "lr": lr, "groupby": f"attr=d.mode=deep.groups-on-page={query.depth}"},
            )
            resp.raise_for_status()
            xml_text = resp.text
        else:
            payload = {
                "query": {
                    "searchType": "SEARCH_TYPE_RU",
                    "queryText": query.keyword(),
                },
                "folderId": env.yandex_search_folder_id,
                "region": str(lr),
                "groupSpec": {
                    "groupMode": "GROUP_MODE_DEEP",
                    "groupsOnPage": min(max(query.depth, 10), 50),
                },
                "responseFormat": "FORMAT_XML",
            }
            resp = await client.post(
                "https://searchapi.api.cloud.yandex.net/v2/web/search",
                headers={"Authorization": f"Api-Key {env.yandex_search_api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_b64 = data.get("rawData", "")
            xml_text = base64.b64decode(raw_b64).decode("utf-8", errors="replace") if raw_b64 else ""

        return parse_yandex_xml(xml_text, query=query)


def parse_yandex_xml(xml_text: str, query: DiscoveryQuery) -> list[DiscoveredPractice]:
    """Разобрать XML-ответ выдачи Яндекса в список практик с позициями."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Не удалось разобрать XML выдачи Яндекса: %s", exc)
        return []

    results: list[DiscoveredPractice] = []
    rank = 0
    for group in root.findall(".//group"):
        doc = group.find("doc")
        if doc is None:
            continue
        rank += 1
        url_el = doc.find("url")
        title_el = doc.find("title")
        url = (url_el.text or "").strip() if url_el is not None else ""
        raw_title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        dom = normalize_domain_idna(url)
        if not dom:
            continue
        name = clean_practice_name(raw_title, dom)
        results.append(
            DiscoveredPractice(
                name=name,
                domain=dom,
                website=f"https://{display_domain(dom)}",
                legal_form=detect_legal_form(raw_title),
                practice_area=query.term,
                city=query.city,
                has_website=True,
                yandex_rank=rank,
                keyword=query.keyword(),
                source="discover:yandex_search",
                source_url=url,
            )
        )
    return results


class DuckDuckGoRuProvider(DiscoveryProvider):
    """Бесплатный веб-поиск по русскоязычным запросам без API-ключей."""

    key = "duckduckgo_ru"
    label = "Веб-поиск RU (DuckDuckGo — бесплатно, без ключа)"
    kind = "serp"
    blurb = "Бесплатный поиск сайтов адвокатов и коллегий по городу и специализации без ключей API."
    cost_per_query_rub = 0.0

    async def fetch(
        self, client: httpx.AsyncClient, env: EnvConfig, query: DiscoveryQuery
    ) -> list[DiscoveredPractice]:
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query.keyword(), "kl": "ru-ru"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        out: list[DiscoveredPractice] = []
        seen_domains: set[str] = set()
        for a_tag in soup.select("a.result__a"):
            href = a_tag.get("href", "")
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                href = unquote(qs.get("uddg", [href])[0])
            dom = normalize_domain_idna(href)
            if not dom or dom in seen_domains:
                continue
            seen_domains.add(dom)
            title = a_tag.get_text(" ", strip=True)
            out.append(
                DiscoveredPractice(
                    name=clean_practice_name(title, dom),
                    domain=dom,
                    website=f"https://{display_domain(dom)}",
                    legal_form=detect_legal_form(title),
                    practice_area=query.term,
                    city=query.city,
                    has_website=True,
                    keyword=query.keyword(),
                    source="discover:duckduckgo_ru",
                    source_url=href,
                )
            )
            if len(out) >= query.limit:
                break
        return out


class TwoGisProvider(DiscoveryProvider):
    """2ГИС Catalog API 3.0 — карточки адвокатов, рейтинг, отзывы, мессенджеры и наличие сайта."""

    key = "twogis"
    label = "2ГИС Справочник организаций"
    kind = "listings"
    blurb = (
        "Ищет адвокатские коллегии, бюро и кабинеты в 2ГИС: собирает телефон, сайт (или его отсутствие), "
        "рейтинг, число отзывов, Telegram/WhatsApp и полноту карточки."
    )
    cost_per_query_rub = 0.0
    env_keys = ("twogis_api_key",)

    async def fetch(
        self, client: httpx.AsyncClient, env: EnvConfig, query: DiscoveryQuery
    ) -> list[DiscoveredPractice]:
        params: dict[str, str | int] = {
            "q": query.keyword(),
            "type": "branch",
            "page_size": min(query.limit, 50),
            "fields": "items.point,items.reviews,items.contact_groups,items.schedule,items.rubrics,items.address_name",
            "key": env.twogis_api_key,
        }
        coords = RU_CITY_COORDS.get(query.city.strip().lower())
        if coords:
            lat, lon = coords
            params["point"] = f"{lon},{lat}"
            params["radius"] = 35000

        resp = await client.get("https://catalog.api.2gis.com/3.0/items", params=params)
        resp.raise_for_status()
        data = resp.json()
        items = ((data.get("result") or {}).get("items")) or []
        return parse_twogis_items(items, query=query)


def parse_twogis_items(items: list[dict], query: DiscoveryQuery) -> list[DiscoveredPractice]:
    """Преобразовать элементы ответа 2ГИС API в карточки практик."""
    out: list[DiscoveredPractice] = []
    for item in items:
        item_id = str(item.get("id") or "").strip()
        name = (item.get("name") or "").strip()
        if not name:
            continue

        website, phone, email, tg, wa, vk = "", "", "", "", "", ""
        for group in item.get("contact_groups") or []:
            for c in group.get("contacts") or []:
                ctype = (c.get("type") or "").lower()
                val = (c.get("value") or c.get("url") or "").strip()
                if ctype == "website" and not website:
                    website = c.get("url") or val
                elif ctype == "phone" and not phone:
                    phone = val
                elif ctype == "email" and not email:
                    email = val
                elif ctype == "telegram" and not tg:
                    tg = val
                elif ctype == "whatsapp" and not wa:
                    wa = val
                elif ctype in ("vkontakte", "vk") and not vk:
                    vk = val

        reviews = item.get("reviews") or {}
        rating = reviews.get("general_rating")
        review_count = reviews.get("general_review_count")
        has_schedule = bool(item.get("schedule"))
        rubrics = [r.get("name", "") for r in (item.get("rubrics") or []) if r.get("name")]

        dom = normalize_domain_idna(website)
        has_site = bool(dom)
        incomplete = (not has_site) or (not has_schedule) or (len(rubrics) == 0)
        twogis_link = f"https://2gis.ru/firm/{item_id.split('_')[0]}" if item_id else ""

        out.append(
            DiscoveredPractice(
                name=name,
                domain=dom,
                website=f"https://{display_domain(dom)}" if dom else "",
                legal_form=detect_legal_form(name + " " + " ".join(rubrics)),
                practice_area=query.term,
                city=query.city,
                address=(item.get("address_name") or "").strip(),
                phone=phone,
                email=email,
                telegram=tg,
                whatsapp=wa,
                vk=vk,
                twogis_url=twogis_link,
                rating=float(rating) if rating is not None else None,
                review_count=int(review_count) if review_count is not None else None,
                has_website=has_site,
                card_incomplete=incomplete,
                keyword=query.keyword(),
                external_id=f"2gis:{item_id}" if item_id else "",
                source="discover:twogis",
                source_url=twogis_link,
            )
        )
    return out


class YandexMapsProvider(DiscoveryProvider):
    """Яндекс Поиск по организациям (Яндекс Карты API v1)."""

    key = "yandex_maps"
    label = "Яндекс Карты (Поиск по организациям)"
    kind = "listings"
    blurb = (
        "Ищет карточки адвокатов, бюро и коллегий в Яндекс Картах: собирает сайт, телефон, "
        "адрес, категории, график и выявляет незаполненные карточки или отсутствие сайта."
    )
    cost_per_query_rub = 0.0
    env_keys = ("yandex_maps_api_key",)

    async def fetch(
        self, client: httpx.AsyncClient, env: EnvConfig, query: DiscoveryQuery
    ) -> list[DiscoveredPractice]:
        params: dict[str, str | int] = {
            "apikey": env.yandex_maps_api_key,
            "text": query.keyword(),
            "lang": "ru_RU",
            "type": "biz",
            "results": min(query.limit, 50),
        }
        coords = RU_CITY_COORDS.get(query.city.strip().lower())
        if coords:
            lat, lon = coords
            params["ll"] = f"{lon},{lat}"
            params["spn"] = "0.55,0.55"

        resp = await client.get("https://search-maps.yandex.ru/v1/", params=params)
        resp.raise_for_status()
        features = (resp.json().get("features")) or []
        return parse_yandex_maps_features(features, query=query)


def parse_yandex_maps_features(features: list[dict], query: DiscoveryQuery) -> list[DiscoveredPractice]:
    """Преобразовать GeoJSON-ответ Яндекс Карт в список практик."""
    out: list[DiscoveredPractice] = []
    for feat in features:
        props = feat.get("properties") or {}
        meta = props.get("CompanyMetaData") or {}
        org_id = str(meta.get("id") or "").strip()
        name = (meta.get("name") or props.get("name") or "").strip()
        if not name:
            continue

        website = (meta.get("url") or "").strip()
        phones = meta.get("Phones") or []
        phone = (phones[0].get("formatted") or "").strip() if phones else ""
        cats = [c.get("name", "") for c in (meta.get("Categories") or []) if c.get("name")]
        has_hours = bool(meta.get("Hours"))
        address = (meta.get("address") or "").strip()

        # Некоторые прокси/обогащённые ответы передают рейтинг и отзывы внутри properties/CompanyMetaData
        rating = meta.get("rating") or (meta.get("Reviews") or {}).get("rating")
        reviews = meta.get("reviews") or (meta.get("Reviews") or {}).get("count")

        dom = normalize_domain_idna(website)
        has_site = bool(dom)
        ymaps_link = f"https://yandex.ru/maps/org/{org_id}/" if org_id else ""

        out.append(
            DiscoveredPractice(
                name=name,
                domain=dom,
                website=f"https://{display_domain(dom)}" if dom else "",
                legal_form=detect_legal_form(name + " " + " ".join(cats)),
                practice_area=query.term,
                city=query.city,
                address=address,
                phone=phone,
                ymaps_url=ymaps_link,
                rating=float(rating) if rating is not None else None,
                review_count=int(reviews) if reviews is not None else None,
                has_website=has_site,
                card_incomplete=(not has_site) or (not has_hours),
                keyword=query.keyword(),
                external_id=f"ymaps:{org_id}" if org_id else "",
                source="discover:yandex_maps",
                source_url=ymaps_link,
            )
        )
    return out


class OverpassRuProvider(DiscoveryProvider):
    """OpenStreetMap Overpass API по городам РФ — бесплатно, без регистрации и ключей."""

    key = "osm_ru"
    label = "OpenStreetMap РФ (адвокаты и юрофисы — бесплатно, без ключа)"
    kind = "listings"
    blurb = "Бесплатная выгрузка карточек адвокатских кабинетов, коллегий и бюро по координатам города РФ."
    cost_per_query_rub = 0.0

    async def fetch(
        self, client: httpx.AsyncClient, env: EnvConfig, query: DiscoveryQuery
    ) -> list[DiscoveredPractice]:
        coords = RU_CITY_COORDS.get(query.city.strip().lower())
        if not coords and query.city:
            coords = await _geocode_nominatim(client, query.city)
        if not coords:
            coords = RU_CITY_COORDS["москва"]
        lat, lon = coords
        overpass_ql = f"""
        [out:json][timeout:25];
        (
          node["office"="lawyer"](around:25000,{lat},{lon});
          way["office"="lawyer"](around:25000,{lat},{lon});
        );
        out center tags {min(query.limit, 100)};
        """
        resp = await client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": overpass_ql},
        )
        resp.raise_for_status()
        elements = resp.json().get("elements") or []
        out: list[DiscoveredPractice] = []
        for el in elements:
            tags = el.get("tags") or {}
            if tags.get("lawyer") == "notary" or tags.get("amenity") in ("courthouse", "police"):
                continue
            name = (tags.get("name:ru") or tags.get("name") or "").strip()
            if not name:
                continue
            website = (tags.get("website") or tags.get("contact:website") or "").strip()
            phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
            email = (tags.get("email") or tags.get("contact:email") or "").strip()
            tg = (tags.get("contact:telegram") or tags.get("telegram") or "").strip()
            street = tags.get("addr:street", "")
            house = tags.get("addr:housenumber", "")
            addr = f"{street}, {house}".strip(", ")
            dom = normalize_domain_idna(website)
            osm_id = f"osm:{el.get('type', 'node')}/{el.get('id')}"

            out.append(
                DiscoveredPractice(
                    name=name,
                    domain=dom,
                    website=f"https://{display_domain(dom)}" if dom else "",
                    legal_form=detect_legal_form(name),
                    practice_area=query.term,
                    city=query.city,
                    address=addr,
                    phone=phone,
                    email=email,
                    telegram=tg,
                    has_website=bool(dom),
                    card_incomplete=not bool(dom),
                    external_id=osm_id,
                    source="discover:osm_ru",
                    source_url=f"https://www.openstreetmap.org/{el.get('type', 'node')}/{el.get('id')}",
                )
            )
        return out


async def _geocode_nominatim(client: httpx.AsyncClient, city: str) -> tuple[float, float] | None:
    try:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{city}, Россия", "format": "json", "limit": 1},
            headers={"User-Agent": "FRIDAY-Legal-Scout/0.1 (github.com/Everick-Ven/F.R.I.D.A.Y.)"},
        )
        r.raise_for_status()
        hits = r.json()
        if hits:
            return float(hits[0]["lat"]), float(hits[0]["lon"])
    except Exception as exc:
        logger.warning("Геокодинг не удался для %r: %s", city, exc)
    return None


PROVIDERS: dict[str, DiscoveryProvider] = {
    p.key: p
    for p in (
        YandexSearchProvider(),
        TwoGisProvider(),
        YandexMapsProvider(),
        DuckDuckGoRuProvider(),
        OverpassRuProvider(),
    )
}

DEFAULT_PROVIDER = "duckduckgo_ru"


def build_queries_from_icp(config: FridayConfig) -> list[DiscoveryQuery]:
    queries: list[DiscoveryQuery] = []
    areas = config.icp.practice_areas or ["адвокат"]
    cities = config.icp.cities or ["Москва"]
    for city in cities:
        for area in areas:
            queries.append(DiscoveryQuery(term=area, city=city))
    return queries


async def persist_discovered_practices(
    state: StateManager,
    items: list[DiscoveredPractice],
    *,
    config: FridayConfig | None = None,
    run_id: str = "",
) -> DiscoveryReport:
    """Сохранить найденные карточки практик и наблюдения в SQLite с проверкой мусора и стоп-листа."""
    from friday.collectors.people import split_russian_fio

    report = DiscoveryReport(found_raw=len(items))
    dnc = getattr(getattr(config, "compliance", None), "do_not_contact", None) or []

    for item in items:
        if is_junk_ru(item.domain, item.name):
            report.skipped_junk += 1
            continue

        if match_do_not_contact(
            domain=item.domain, email=item.email, telegram=item.telegram, entries=dnc
        ):
            report.skipped_dnc += 1
            continue

        # Пропускаем адвокатов с недействующим статусом из реестров
        status_low = (item.registry_status or "").strip().lower()
        if status_low and any(w in status_low for w in ("прекращ", "приостанов", "отмен", "удален", "исключ")):
            report.skipped_junk += 1
            continue

        if not item.domain and not item.external_id:
            slug = re.sub(r"\W+", "_", item.name.lower())[:40]
            item.external_id = f"manual:{slug}:{item.city.lower()}"

        comp = Company(
            name=item.name or display_domain(item.domain),
            domain=item.domain,
            website=item.website,
            legal_form=item.legal_form or detect_legal_form(item.name),
            practice_areas=[item.practice_area] if item.practice_area else [],
            city=item.city,
            address=item.address,
            phone=item.phone,
            telegram=item.telegram,
            whatsapp=item.whatsapp,
            vk=item.vk,
            ymaps_url=item.ymaps_url,
            twogis_url=item.twogis_url,
            external_id=item.external_id,
            source=item.source,
            source_url=item.source_url or item.website,
        )
        cid, is_new = await state.upsert_company(comp)
        if is_new:
            report.new_companies += 1
        else:
            report.updated_companies += 1

        ev_url = item.source_url or item.website
        # Записываем наблюдения (только подтверждённые пользователем сигналы попадут в БД)
        obs_candidates: list[tuple[str, float | None, str]] = []
        if item.yandex_rank is not None:
            obs_candidates.append(("YANDEX_SERP_RANK", float(item.yandex_rank), item.keyword))
            obs_candidates.append(("FOUND_IN_YANDEX", None, f"{item.keyword} (#{item.yandex_rank})"))
        elif item.keyword:
            obs_candidates.append(("FOUND_IN_YANDEX", None, item.keyword))

        if item.source == "discover:yandex_maps":
            if item.rating is not None:
                obs_candidates.append(("YANDEX_MAPS_RATING", float(item.rating), item.name))
            if item.review_count is not None:
                obs_candidates.append(("YANDEX_MAPS_REVIEWS", float(item.review_count), item.name))
        if item.source == "discover:twogis":
            if item.rating is not None:
                obs_candidates.append(("TWOGIS_RATING", float(item.rating), item.name))
            if item.review_count is not None:
                obs_candidates.append(("TWOGIS_REVIEWS", float(item.review_count), item.name))

        if item.has_website is False:
            obs_candidates.append(("NO_WEBSITE", 1.0, "Нет сайта в карточке"))
        if item.card_incomplete:
            obs_candidates.append(("UNCLAIMED_OR_EMPTY_CARD", 1.0, "Незаполненная карточка"))
        if item.registry_number:
            obs_candidates.append(
                ("MINJUST_REGISTRY_ACTIVE", None, f"{item.registry_number} ({item.registry_status or 'действующий'})")
            )
        if item.telegram:
            obs_candidates.append(("TELEGRAM_CONTACT", None, item.telegram))
        if item.email:
            obs_candidates.append(("PUBLIC_EMAIL_CONTACT", None, item.email))

        for code, vnum, vtext in obs_candidates:
            written = await state.record_observation(
                signal_code=code,
                company_id=cid,
                collector="discover",
                value_num=vnum,
                value_text=vtext,
                evidence_url=ev_url,
                run_id=run_id,
            )
            if written:
                report.observations_written += 1

        # Если сразу известны ФИО адвоката (из реестра или именного импорта) — создаём контакт
        if item.advocate_fio:
            parsed_fio = split_russian_fio(item.advocate_fio)
            if parsed_fio:
                ln, fn, mn = parsed_fio
                _, p_new = await state.upsert_prospect(
                    Prospect(
                        company_id=cid,
                        last_name=ln,
                        first_name=fn,
                        middle_name=mn,
                        title="Адвокат",
                        role_kind="solo_advocate",
                        registry_number=item.registry_number,
                        registry_status=item.registry_status or ("действующий" if item.registry_number else ""),
                        email=item.email,
                        email_source="registry_or_import" if item.email else "",
                        phone=item.phone,
                        telegram=item.telegram,
                        whatsapp=item.whatsapp,
                        company=comp.name,
                        city=item.city,
                        source=item.source,
                        source_url=ev_url,
                    )
                )
                if p_new:
                    report.new_prospects += 1

    return report


async def run_discovery(
    state: StateManager,
    config: FridayConfig,
    env: EnvConfig,
    *,
    provider_key: str = DEFAULT_PROVIDER,
    queries: list[DiscoveryQuery] | None = None,
    max_spend_rub: float | None = None,
    dry_run: bool = False,
) -> DiscoveryReport:
    """Выполнить стадию DISCOVER выбранным провайдером с предварительной оценкой бюджета."""
    if provider_key not in PROVIDERS:
        raise ValueError(
            f"Неизвестный провайдер `{provider_key}`. Доступны: {', '.join(PROVIDERS)}"
        )
    provider = PROVIDERS[provider_key]
    q_list = queries or build_queries_from_icp(config)
    budget = max_spend_rub if max_spend_rub is not None else config.usage.discover_max_spend_rub

    report = DiscoveryReport(
        provider=provider_key,
        estimated_cost_rub=provider.estimate_rub(q_list),
    )

    missing = provider.missing_keys(env)
    if missing:
        report.errors.append(
            f"Провайдер `{provider_key}` требует переменные в .env: {', '.join(missing)}"
        )
        return report

    if dry_run:
        return report

    run_id = await state.start_run(
        stage="discover",
        provider=provider_key,
        params={"queries": [q.keyword() for q in q_list], "budget_rub": budget},
    )

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            for idx, q in enumerate(q_list):
                if report.actual_cost_rub + provider.cost_per_query_rub > budget:
                    report.errors.append(f"Достигнут лимит бюджета разведки ({budget:.2f} ₽)")
                    break
                if idx > 0 and provider_key in ("duckduckgo_ru", "osm_ru"):
                    await asyncio.sleep(1.2)
                try:
                    batch = await provider.fetch(client, env, q)
                    report.queries_run += 1
                    report.actual_cost_rub += provider.cost_per_query_rub
                    sub = await persist_discovered_practices(
                        state, batch, config=config, run_id=run_id
                    )
                    report.found_raw += sub.found_raw
                    report.new_companies += sub.new_companies
                    report.updated_companies += sub.updated_companies
                    report.new_prospects += sub.new_prospects
                    report.observations_written += sub.observations_written
                    report.skipped_junk += sub.skipped_junk
                    report.skipped_dnc += sub.skipped_dnc
                except Exception as exc:
                    logger.warning("Ошибка запроса %r (%s): %s", q.keyword(), provider_key, exc)
                    report.errors.append(f"{q.keyword()}: {exc}")

        await state.finish_run(
            run_id,
            status="completed" if not report.errors else "completed_with_errors",
            records=report.new_companies,
            cost_rub=report.actual_cost_rub,
            error="; ".join(report.errors[:3]),
        )
    except Exception as exc:
        await state.finish_run(run_id, status="failed", error=str(exc))
        raise

    return report


# ── Импорт реестров адвокатов регионов РФ и собственных списков пользователя ──

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "company", "company_name", "название", "компания", "адвокатское образование", "образование", "организация", "бюро", "коллегия"),
    "fio": ("fio", "advocate", "full_name", "person", "фио", "адвокат", "ф.и.о.", "ф.и.о", "партнер", "партнёр", "контакт"),
    "website": ("website", "url", "site", "domain", "сайт", "домен", "ссылка"),
    "city": ("city", "region", "location", "город", "регион", "субъект"),
    "practice_area": ("practice", "practice_area", "specialty", "специализация", "практика", "направление"),
    "phone": ("phone", "tel", "телефон", "тел"),
    "email": ("email", "mail", "почта", "e-mail"),
    "telegram": ("telegram", "tg", "телеграм", "тг"),
    "registry_number": ("registry_number", "reg_num", "reg_no", "реестровый номер", "номер в реестре", "реестр"),
    "registry_status": ("registry_status", "status", "статус", "статус адвоката"),
    "twogis_url": ("2gis", "twogis", "2гис"),
    "ymaps_url": ("ymaps", "yandex_maps", "яндекс карты", "яндекс.карты"),
    "rating": ("rating", "рейтинг", "оценка"),
    "reviews": ("reviews", "review_count", "отзывы", "кол-во отзывов"),
}


def _map_row_fields(row: dict[str, str]) -> dict[str, str]:
    lowered = {str(k).strip().lower(): str(v).strip() for k, v in row.items() if k is not None and v is not None}
    mapped: dict[str, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered and lowered[alias]:
                mapped[canonical] = lowered[alias]
                break
    return mapped


def parse_import_text(content: str, *, source_tag: str = "import:custom_list", default_city: str = "") -> list[DiscoveredPractice]:
    """Разобрать CSV, TSV, JSON, HTML-таблицу реестра или текстовый список ссылок."""
    stripped = content.strip()
    if not stripped:
        return []

    # 1. JSON (список словарей)
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            rows = parsed if isinstance(parsed, list) else (parsed.get("items") or parsed.get("lawyers") or [parsed])
            return [_practice_from_mapped(_map_row_fields(r), source_tag, default_city) for r in rows if isinstance(r, dict)]
        except ValueError:
            pass

    # 2. HTML-таблица регионального реестра адвокатов (например, выгрузка со страницы палаты / Минюста)
    if "<table" in stripped.lower() and "<tr" in stripped.lower():
        soup = BeautifulSoup(stripped, "html.parser")
        table = soup.find("table")
        if table:
            headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
            items: list[DiscoveredPractice] = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if not cells:
                    continue
                if headers and len(headers) == len(cells):
                    row_dict = dict(zip(headers, cells))
                else:
                    # Типичный порядок колонок реестра: ФИО | Реестровый номер | Статус | Образование
                    row_dict = {
                        "фио": cells[0] if len(cells) > 0 else "",
                        "реестровый номер": cells[1] if len(cells) > 1 else "",
                        "статус": cells[2] if len(cells) > 2 else "",
                        "адвокатское образование": cells[3] if len(cells) > 3 else "",
                    }
                items.append(_practice_from_mapped(_map_row_fields(row_dict), source_tag, default_city))
            return [x for x in items if x.name or x.domain or x.advocate_fio]

    # 3. CSV / TSV с заголовками (поддержка `;`, `,` и `\t` — стандарт русского Excel)
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    first_line = lines[0]
    if any(sep in first_line for sep in (";", ",", "\t")) and len(lines) >= 2:
        sep = ";" if first_line.count(";") >= max(first_line.count(","), first_line.count("\t")) else ("\t" if "\t" in first_line else ",")
        reader = csv.DictReader(io.StringIO(stripped), delimiter=sep)
        if reader.fieldnames and any(_map_row_fields({fn: "x"}) for fn in reader.fieldnames if fn):
            out = []
            for r in reader:
                m = _map_row_fields(r)
                if m:
                    out.append(_practice_from_mapped(m, source_tag, default_city))
            if out:
                return out

    # 4. Простой построчный список доменов / URL / @telegram
    out_plain: list[DiscoveredPractice] = []
    for ln in lines:
        val = ln.strip()
        if not val or val.startswith("#"):
            continue
        if val.startswith("@") or "t.me/" in val:
            handle = val if val.startswith("@") else "@" + val.split("t.me/", 1)[1].split("/")[0]
            out_plain.append(
                DiscoveredPractice(
                    name=f"Практика ({handle})",
                    telegram=handle,
                    city=default_city,
                    has_website=False,
                    external_id=f"tg:{handle.lower()}",
                    source=source_tag,
                )
            )
        else:
            dom = normalize_domain_idna(val)
            if dom:
                out_plain.append(
                    DiscoveredPractice(
                        name=display_domain(dom),
                        domain=dom,
                        website=f"https://{display_domain(dom)}",
                        city=default_city,
                        has_website=True,
                        source=source_tag,
                        source_url=val if "://" in val else f"https://{display_domain(dom)}",
                    )
                )
    return out_plain


def _practice_from_mapped(m: dict[str, str], source_tag: str, default_city: str) -> DiscoveredPractice:
    website = m.get("website", "")
    dom = normalize_domain_idna(website)
    fio = m.get("fio", "")
    reg_no = m.get("registry_number", "")
    if not reg_no and fio:
        reg_match = REGISTRY_NUMBER_RE.search(fio)
        if reg_match:
            reg_no = reg_match.group(1)
            fio = REGISTRY_NUMBER_RE.sub("", fio).strip(" ,;-()")

    comp_name = m.get("name", "")
    if not comp_name:
        if fio:
            comp_name = f"Адвокатский кабинет — {fio}"
        elif dom:
            comp_name = display_domain(dom)

    rating = None
    if m.get("rating"):
        try:
            rating = float(m["rating"].replace(",", "."))
        except ValueError:
            pass
    reviews = None
    if m.get("reviews"):
        digits = re.sub(r"\D", "", m["reviews"])
        if digits:
            reviews = int(digits)

    ext_id = ""
    if reg_no:
        ext_id = f"minjust:{reg_no}"
    elif not dom and comp_name:
        slug = re.sub(r"\W+", "_", comp_name.lower())[:40]
        ext_id = f"{source_tag}:{slug}"

    source = "discover:twogis" if m.get("twogis_url") else (
        "discover:yandex_maps" if m.get("ymaps_url") else source_tag
    )

    return DiscoveredPractice(
        name=comp_name,
        domain=dom,
        website=f"https://{display_domain(dom)}" if dom else "",
        legal_form=detect_legal_form(comp_name),
        practice_area=m.get("practice_area", ""),
        city=m.get("city", "") or default_city,
        phone=m.get("phone", ""),
        email=m.get("email", ""),
        telegram=m.get("telegram", ""),
        twogis_url=m.get("twogis_url", ""),
        ymaps_url=m.get("ymaps_url", ""),
        rating=rating,
        review_count=reviews,
        has_website=bool(dom) if (dom or reg_no or m.get("phone")) else None,
        external_id=ext_id,
        source=source,
        source_url=website or m.get("twogis_url", "") or m.get("ymaps_url", ""),
        advocate_fio=fio,
        registry_number=reg_no,
        registry_status=m.get("registry_status", ""),
    )


async def import_practices_from_file(
    state: StateManager,
    path: str | Path,
    *,
    config: FridayConfig | None = None,
    is_registry: bool = False,
    default_city: str = "",
) -> DiscoveryReport:
    """Импортировать собственный файл пользователя или выгрузку регионального реестра адвокатов."""
    text = read_user_text(path)
    tag = "discover:minjust_registry" if is_registry else "import:custom_list"
    items = parse_import_text(text, source_tag=tag, default_city=default_city)
    run_id = await state.start_run(
        stage="import_registry" if is_registry else "import_list",
        provider=tag,
        params={"file": str(path), "rows": len(items)},
    )
    report = await persist_discovered_practices(state, items, config=config, run_id=run_id)
    report.provider = tag
    await state.finish_run(run_id, status="completed", records=report.new_companies)
    return report
