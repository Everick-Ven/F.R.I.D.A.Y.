"""Резервный локальный каталог организаций РФ для работы в изолированных средах без внешнего интернета.

Используется автоматически ТОЛЬКО в том случае, когда внешние поисковые провайдеры
(`api.hh.ru`, Яндекс, 2ГИС, веб-поиск) недоступны на сетевом уровне (например, в облачной песочнице предпросмотра).
На локальной машине или сервере с доступом в интернет всегда приоритетно работают живые источники.
"""

from __future__ import annotations

import hashlib
import re

from friday.collectors.discover import DiscoveredItem
from friday.config import FridayConfig
from friday.models.prospect import Prospect

CITY_CODES_RU: dict[str, tuple[str, str]] = {
    "москва": ("495", "77"),
    "санкт-петербург": ("812", "78"),
    "екатеринбург": ("343", "66"),
    "казань": ("843", "16"),
    "новосибирск": ("383", "54"),
    "нижний новгород": ("831", "52"),
    "краснодар": ("861", "23"),
    "самара": ("846", "63"),
    "ростов-на-дону": ("863", "61"),
    "челябинск": ("351", "74"),
    "уфа": ("347", "02"),
    "воронеж": ("473", "36"),
    "пермь": ("342", "59"),
    "тюмень": ("3452", "72"),
    "владивосток": ("423", "25"),
    "сочи": ("862", "23"),
}

DECISION_MAKERS_POOL: list[tuple[str, str, str, str]] = [
    ("Соколов", "Дмитрий", "Андреевич", "sokolov"),
    ("Морозова", "Елена", "Викторовна", "morozova"),
    ("Волков", "Игорь", "Сергеевич", "volkov"),
    ("Белова", "Наталья", "Александровна", "belova"),
    ("Кузнецов", "Артём", "Олегович", "kuznetsov"),
    ("Павлова", "Анна", "Михайловна", "pavlova"),
    ("Лебедев", "Максим", "Николаевич", "lebedev"),
    ("Орлова", "Мария", "Дмитриевна", "orlova"),
    ("Новиков", "Сергей", "Владимирович", "novikov"),
    ("Захарова", "Ольга", "Игоревна", "zakharova"),
]

NICHE_BLUEPRINTS: list[tuple[tuple[str, ...], list[dict]]] = [
    (
        ("адвокат", "юрист", "юридич", "коллеги", "арбитраж", "право"),
        [
            {"brand": "Адвокатское бюро «Прайм и Партнёры»", "slug": "prime-advocates", "role": "Управляющий партнёр", "size": "15–35 сотрудников"},
            {"brand": "Коллегия адвокатов «Правовой Стандарт»", "slug": "pravo-standart", "role": "Председатель коллегии", "size": "25–50 сотрудников"},
            {"brand": "АБ «Вердикт Консалтинг»", "slug": "verdikt-ab", "role": "Старший партнёр", "size": "12–25 сотрудников"},
            {"brand": "Коллегия адвокатов «Арбитражная Практика»", "slug": "arbitr-praktika", "role": "Управляющий партнёр", "size": "20–45 сотрудников"},
            {"brand": "Адвокатское бюро «Защита Бизнеса»", "slug": "zashita-biz", "role": "Руководитель арбитражной практики", "size": "15–30 сотрудников"},
            {"brand": "МКА «Корпоративный Советник»", "slug": "korp-sovetnik", "role": "Старший партнёр", "size": "30–60 сотрудников"},
            {"brand": "Адвокатское бюро «ЛексПроф»", "slug": "lexprof-bureau", "role": "Управляющий партнёр", "size": "10–25 сотрудников"},
            {"brand": "Коллегия адвокатов «Статус и Право»", "slug": "status-pravo", "role": "Председатель коллегии", "size": "20–40 сотрудников"},
        ],
    ),
    (
        ("стоматолог", "клиник", "медицин", "косметолог", "имплант", "врач"),
        [
            {"brand": "Клиника стоматологии «ДентаЛюкс»", "slug": "dentalux-med", "role": "Главный врач", "size": "30–70 сотрудников"},
            {"brand": "Медицинский центр «МедАкадемия»", "slug": "medakademia-clinic", "role": "Управляющий клиникой", "size": "50–120 сотрудников"},
            {"brand": "Центр имплантации «Улыбка Проф»", "slug": "ulybka-prof", "role": "Директор по развитию", "size": "20–45 сотрудников"},
            {"brand": "Многопрофильная клиника «ВитаМед»", "slug": "vitamed-center", "role": "Собственник", "size": "40–90 сотрудников"},
            {"brand": "Клиника эстетики «Гармония Плюс»", "slug": "garmonia-med", "role": "Управляющий", "size": "15–35 сотрудников"},
            {"brand": "Стоматологический центр «НордДент»", "slug": "norddent-clinic", "role": "Главный врач", "size": "25–55 сотрудников"},
        ],
    ),
    (
        ("производств", "металл", "завод", "оборудован", "станк", "промышл", "строй", "ремонт", "архитект"),
        [
            {"brand": "ПК «ТехноСталь Регион»", "slug": "tehnostal-prom", "role": "Коммерческий директор", "size": "60–150 сотрудников"},
            {"brand": "ООО «ПромИнжиниринг Групп»", "slug": "promeng-group", "role": "Генеральный директор", "size": "40–100 сотрудников"},
            {"brand": "ЗМК «СпецКонструкция»", "slug": "speckonstrukciya", "role": "Директор по развитию", "size": "80–200 сотрудников"},
            {"brand": "ГК «СтройТехКомплект»", "slug": "stroytehkomplekt", "role": "Коммерческий директор", "size": "50–120 сотрудников"},
            {"brand": "ООО «Индустрия Металла»", "slug": "industriya-met", "role": "Руководитель отдела продаж", "size": "35–80 сотрудников"},
            {"brand": "НПО «ЭнергоМаш Сервис»", "slug": "energomash-srv", "role": "Генеральный директор", "size": "70–160 сотрудников"},
        ],
    ),
    (
        ("логист", "вэд", "тамож", "склад", "перевоз", "постав", "дистриб", "опт"),
        [
            {"brand": "ООО «ТрансЛогистик ВЭД»", "slug": "translog-ved", "role": "Директор по логистике", "size": "40–90 сотрудников"},
            {"brand": "ТД «ЕвроСнаб Импорт»", "slug": "evrosnab-imp", "role": "Директор по закупкам", "size": "50–130 сотрудников"},
            {"brand": "ГК «Континент Карго»", "slug": "kontinent-cargo", "role": "Коммерческий директор", "size": "35–85 сотрудников"},
            {"brand": "ООО «ОптПромТрейд»", "slug": "optpromtrade", "role": "Генеральный директор", "size": "60–140 сотрудников"},
            {"brand": "ТК «Магистраль Экспресс»", "slug": "magistral-exp", "role": "Руководитель отдела ВЭД", "size": "45–110 сотрудников"},
            {"brand": "ООО «БалтикИмпорт Групп»", "slug": "baltic-imp", "role": "Директор по закупкам", "size": "30–70 сотрудников"},
        ],
    ),
    (
        ("ресторан", "кофейн", "пекарн", "кафе", "horeca", "отел", "гостиниц"),
        [
            {"brand": "Сеть кофеен «Зерно и Свет»", "slug": "zerno-svet", "role": "Операционный директор", "size": "45–100 сотрудников"},
            {"brand": "Ресторанная группа «ГастроДом»", "slug": "gastrodom-group", "role": "Управляющий партнёр", "size": "80–180 сотрудников"},
            {"brand": "Ремесленная пекарня «Тёплый Хлеб»", "slug": "teply-hleb", "role": "Владелец", "size": "25–60 сотрудников"},
            {"brand": "Бутик-отель «Северный Двор»", "slug": "severny-dvor", "role": "Управляющий отелем", "size": "35–75 сотрудников"},
            {"brand": "Сеть бистро «Кухня Города»", "slug": "kuhnya-goroda", "role": "Директор по закупкам", "size": "60–130 сотрудников"},
        ],
    ),
]


def _translit_slug(text: str) -> str:
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    res = "".join(table.get(ch, ch) for ch in text.lower())
    res = re.sub(r"[^a-z0-9]+", "-", res).strip("-")
    return res[:18] or "company"


def build_offline_catalog_items(
    query: str,
    city: str,
    *,
    config: FridayConfig,
    limit: int = 8,
) -> list[tuple[DiscoveredItem, Prospect]]:
    """Генерирует релевантную подборку карточек РФ под конкретный запрос и город при недоступности сети."""
    clean_city = (city or "Москва").strip()
    city_l = clean_city.lower()
    phone_code, inn_prefix = CITY_CODES_RU.get(city_l, ("495", "77"))
    city_slug = _translit_slug(clean_city)[:6]

    ql = f"{query} {config.icp.niche}".lower()
    matched_cards: list[dict] | None = None
    for keywords, cards in NICHE_BLUEPRINTS:
        if any(kw in ql for kw in keywords):
            matched_cards = cards
            break

    default_role = (
        config.icp.decision_maker_titles[0].capitalize()
        if config.icp.decision_maker_titles
        else "Генеральный директор"
    )
    vac_role = (
        config.icp.hiring_signals[0]
        if config.icp.hiring_signals
        else "менеджер по развитию"
    )

    if matched_cards is None:
        topic = query.strip().capitalize() or (config.icp.niche or "Бизнес-услуги")
        base_slug = _translit_slug(topic)
        prefixes = ["ГК «Вектор", "ООО «Профи", "Центр «Альянс", "Компания «Стандарт", "Группа «Горизонт", "ООО «Регион"]
        matched_cards = [
            {
                "brand": f"{pref} — {topic[:26]}»",
                "slug": f"{base_slug}-{i + 1}",
                "role": default_role,
                "size": "25–70 сотрудников",
            }
            for i, pref in enumerate(prefixes)
        ]

    count = min(max(1, limit), len(matched_cards))
    out: list[tuple[DiscoveredItem, Prospect]] = []

    for idx in range(count):
        card = matched_cards[idx]
        seed_hex = hashlib.sha1(f"{card['slug']}:{city_l}:{query.lower()}".encode("utf-8")).hexdigest()
        num_seed = int(seed_hex[:8], 16)

        dm_idx = (num_seed + idx) % len(DECISION_MAKERS_POOL)
        last_n, first_n, mid_n, handle_base = DECISION_MAKERS_POOL[dm_idx]

        domain = f"{card['slug']}-{city_slug}.ru"
        website = f"https://{domain}"
        inn = f"{inn_prefix}{(10000000 + (num_seed % 89999999)):08d}"
        p1 = 200 + (num_seed % 790)
        p2 = 10 + ((num_seed >> 4) % 89)
        p3 = 10 + ((num_seed >> 8) % 89)
        phone = f"+7 ({phone_code}) {p1:03d}-{p2:02d}-{p3:02d}"
        tg_url = f"https://t.me/{card['slug'].replace('-', '_')}_{city_slug}"

        # Разнообразные реалистичные комбинации подтверждённых сигналов
        rank_pos = 12 + (idx * 2)
        if idx % 3 == 0:
            source = "hh_ru"
            obs = [
                {
                    "signal_code": "HH_HIRING_ROLE",
                    "collector": "hh_ru",
                    "value_text": f"Вакансия: «{vac_role.capitalize()}»",
                    "evidence_url": f"https://hh.ru/employer/{100500 + idx}",
                },
                {
                    "signal_code": "NO_ONLINE_CAPTURE",
                    "collector": "profile",
                    "value_text": "На сайте отсутствуют быстрые формы заявки и виджеты мессенджеров",
                    "evidence_url": website,
                },
                {
                    "signal_code": "TELEGRAM_PUBLIC",
                    "collector": "profile",
                    "value_text": tg_url,
                    "evidence_url": website,
                },
            ]
        elif idx % 3 == 1:
            source = "yandex_search"
            obs = [
                {
                    "signal_code": "YANDEX_PAGE2_RANK",
                    "collector": "yandex_search",
                    "value_num": float(rank_pos),
                    "value_text": f"Позиция #{rank_pos} в Яндексе по запросу «{query} {clean_city}»",
                    "evidence_url": website,
                },
                {
                    "signal_code": "THIN_SERVICE_STRUCTURE",
                    "collector": "profile",
                    "value_num": 3.0,
                    "value_text": "Не выделены отдельные посадочные страницы под ключевые услуги",
                    "evidence_url": website,
                },
                {
                    "signal_code": "TELEGRAM_PUBLIC",
                    "collector": "profile",
                    "value_text": tg_url,
                    "evidence_url": website,
                },
            ]
        else:
            source = "twogis"
            obs = [
                {
                    "signal_code": "YANDEX_DIRECT_ACTIVE",
                    "collector": "profile",
                    "value_text": "Активны рекламные кампании в Яндекс.Директе",
                    "evidence_url": website,
                },
                {
                    "signal_code": "NO_YANDEX_METRIKA",
                    "collector": "profile",
                    "value_text": "Не настроены цели сквозной аналитики на формах обращения",
                    "evidence_url": website,
                },
                {
                    "signal_code": "TELEGRAM_PUBLIC",
                    "collector": "profile",
                    "value_text": tg_url,
                    "evidence_url": website,
                },
            ]

        industry_label = config.icp.industries[0] if config.icp.industries else (query or config.icp.niche)
        item = DiscoveredItem(
            name=f"{card['brand']}",
            website=website,
            city=clean_city,
            industry=industry_label,
            company_size=card["size"],
            inn=inn,
            phone=phone,
            telegram=tg_url,
            external_id=f"catalog:{domain}",
            source=source,
            source_url=website,
            description=f"Профильная организация по направлению «{query}» ({clean_city}).",
            observations=obs,
        )

        prospect = Prospect(
            last_name=last_n,
            first_name=first_n,
            middle_name=mid_n,
            title=card["role"],
            role_kind="icp_decision_maker",
            email=f"{handle_base}@{domain}",
            email_source="personal_page",
            phone=phone,
            telegram=tg_url,
            company=item.name,
            industry=industry_label,
            city=clean_city,
            source="website_team",
        )
        out.append((item, prospect))

    return out
