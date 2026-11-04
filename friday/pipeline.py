"""Оркестратор конвейера F.R.I.D.A.Y. (раздельные этапы для любой ниши рынка РФ).

Этапы конвейера (после Шага 1 — настройки ниши и продукта):
  Step 1. `train`     — настройка продукта и целевой ниши (`friday.local.yaml` + `skills/product_knowledge.md`).
  Step 2. `discover`  — поиск целевых компаний (hh.ru, Яндекс, 2ГИС, Яндекс Карты, DDG/OSM, CSV/JSON).
  Step 3. `profile`   — детерминированный технический и маркетинговый профиль сайтов ($0 HTTP).
  Step 4. `prospects` — извлечение ФИО, должностей ЛПР и прямых контактов ($0 HTTP).
  Step 5. `score`     — скоринг соответствия ICP + очередь `scout_score` для ИИ-агента.
  Step 6. `drafts`    — постановка задач `writer_draft` для ИИ-агента рабочей среды + шлюз ФЗ-38 (`gate.py`).
"""

from __future__ import annotations

from friday.agents.scout import score_all_prospects
from friday.agents.writer import run_drafts
from friday.collectors.discover import DiscoveredItem, persist_discovered_items, run_discover
from friday.collectors.people import run_people
from friday.collectors.profile import run_profile
from friday.config import FridayConfig, is_niche_configured, load_config
from friday.models.prospect import Prospect
from friday.signals import ensure_signal_catalog
from friday.state import StateManager


async def bootstrap_state(
    state: StateManager,
    *,
    auto_confirm_core: bool = True,
) -> dict:
    """Инициализирует SQLite (WAL) и каталог из 26 универсальных сигналов РФ."""
    await state.init_db()
    inserted = await ensure_signal_catalog(state, auto_confirm_core=auto_confirm_core)
    return {"signals_seeded": inserted}


async def run_full_pipeline(
    state: StateManager,
    *,
    query: str | None = None,
    city: str | None = None,
    provider: str = "auto",
    limit: int = 20,
    channel: str = "telegram",
    use_ai_scoring: bool = False,
    mode_override: str | None = None,
    config: FridayConfig | None = None,
) -> dict:
    """Запускает цепочку этапов конвейера для настроенной ниши (без какой-либо автоотправки)."""
    cfg = config or load_config()
    await bootstrap_state(state, auto_confirm_core=True)

    q = (query or "").strip()
    if not q and cfg.icp.search_queries:
        q = cfg.icp.search_queries[0]
    if not q:
        raise ValueError(
            "Сначала настройте нишу и поисковые запросы на Шаге 1 (`friday train` или вкладка «1. Ниша и продукт»)."
        )

    c = (city or "").strip()
    if not c:
        c = cfg.icp.cities[0] if cfg.icp.cities else "Москва"

    disc = await run_discover(state, query=q, city=c, provider=provider, limit=limit, config=cfg)
    prof = await run_profile(state, limit=limit)
    peop = await run_people(state, limit=limit, config=cfg)
    scor = await score_all_prospects(state, use_ai=use_ai_scoring, limit=limit * 3, config=cfg)
    drft = await run_drafts(state, channel=channel, limit=limit, mode_override=mode_override, config=cfg)

    return {
        "niche_configured": is_niche_configured(cfg),
        "query": q,
        "city": c,
        "discover": disc,
        "profile": prof,
        "prospects": peop,
        "score": scor,
        "drafts": drft,
    }


async def seed_sample_for_active_niche(
    state: StateManager,
    *,
    config: FridayConfig | None = None,
) -> dict:
    """Явно вызываемый тестовый стенд под текущую настроенную нишу (не запускается автоматически)."""
    cfg = config or load_config()
    await bootstrap_state(state, auto_confirm_core=True)

    city1 = cfg.icp.cities[0] if cfg.icp.cities else "Москва"
    city2 = cfg.icp.cities[1] if len(cfg.icp.cities) > 1 else "Санкт-Петербург"
    ind = cfg.icp.industries[0] if cfg.icp.industries else (cfg.icp.niche or "B2B-компания")
    q_label = cfg.icp.search_queries[0] if cfg.icp.search_queries else ind
    role1 = cfg.icp.decision_maker_titles[0].capitalize() if cfg.icp.decision_maker_titles else "Коммерческий директор"
    role2 = cfg.icp.decision_maker_titles[1].capitalize() if len(cfg.icp.decision_maker_titles) > 1 else "Генеральный директор"
    vac_role = cfg.icp.hiring_signals[0] if cfg.icp.hiring_signals else "менеджер по продажам"

    samples: list[tuple[DiscoveredItem, Prospect | None]] = [
        (
            DiscoveredItem(
                name=f"ООО «Альфа-{ind[:18]}»",
                website="https://alfa-sample-b2b.ru",
                city=city1,
                industry=ind,
                company_size="25–60 сотрудников",
                inn="7704123456",
                phone="+7 (495) 740-11-22",
                telegram="https://t.me/alfa_sample_b2b",
                hh_url="https://hh.ru/employer/100001",
                external_id="sample:alfa_b2b",
                source="hh_ru",
                description=f"Профильная компания сферы «{ind}» в г. {city1}.",
                observations=[
                    {
                        "signal_code": "HH_HIRING_ROLE",
                        "collector": "hh_ru",
                        "value_text": f"Вакансия: «{vac_role.capitalize()}»",
                        "evidence_url": "https://hh.ru/vacancy/900001",
                    },
                    {
                        "signal_code": "YANDEX_DIRECT_ACTIVE",
                        "collector": "profile",
                        "value_text": "Запущены кампании в Яндекс.Директе",
                        "evidence_url": "https://alfa-sample-b2b.ru",
                    },
                    {
                        "signal_code": "NO_ONLINE_CAPTURE",
                        "collector": "profile",
                        "value_text": "На посадочной нет квиза, онлайн-чата и захвата заявок",
                        "evidence_url": "https://alfa-sample-b2b.ru",
                    },
                    {
                        "signal_code": "TELEGRAM_PUBLIC",
                        "collector": "profile",
                        "value_text": "https://t.me/alfa_sample_b2b",
                        "evidence_url": "https://alfa-sample-b2b.ru",
                    },
                ],
            ),
            Prospect(
                last_name="Громов",
                first_name="Алексей",
                middle_name="Викторович",
                title=role1,
                role_kind="icp_decision_maker",
                email="a.gromov@alfa-sample-b2b.ru",
                email_source="personal_page",
                phone="+7 (495) 740-11-22",
                telegram="https://t.me/alfa_sample_b2b",
                industry=ind,
                city=city1,
                source="website_team",
            ),
        ),
        (
            DiscoveredItem(
                name=f"ГК «СеверПром» ({ind[:20]})",
                website="https://severprom-sample.ru",
                city=city2,
                industry=ind,
                company_size="50–120 сотрудников",
                inn="7810654321",
                phone="+7 (812) 602-44-19",
                whatsapp="https://wa.me/78126024419",
                external_id="sample:severprom",
                source="yandex_search",
                description=f"Поставщик и подрядчик направления «{q_label}» в {city2}.",
                observations=[
                    {
                        "signal_code": "YANDEX_PAGE2_RANK",
                        "collector": "yandex_search",
                        "value_num": 14.0,
                        "value_text": f"Позиция #14 в Яндексе по запросу «{q_label} {city2}»",
                        "evidence_url": "https://severprom-sample.ru",
                    },
                    {
                        "signal_code": "THIN_SERVICE_STRUCTURE",
                        "collector": "profile",
                        "value_num": 2.0,
                        "value_text": "Узкая структура (2 внутренние ссылки без отдельных посадочных)",
                        "evidence_url": "https://severprom-sample.ru",
                    },
                    {
                        "signal_code": "TECH_STACK_RU",
                        "collector": "profile",
                        "value_text": "1С-Битрикс",
                        "evidence_url": "https://severprom-sample.ru",
                    },
                ],
            ),
            Prospect(
                last_name="Ковалёва",
                first_name="Ольга",
                middle_name="Андреевна",
                title=role2,
                role_kind="icp_decision_maker",
                email="kovaleva@severprom-sample.ru",
                email_source="personal_page",
                phone="+7 (812) 602-44-19",
                whatsapp="https://wa.me/78126024419",
                industry=ind,
                city=city2,
                source="website_team",
            ),
        ),
    ]

    stats = await persist_discovered_items(state, samples, config=cfg, run_id="sample_seed")
    for c in await state.get_companies(limit=20):
        if c.external_id.startswith("sample:"):
            await state.mark_profile_checked(c.id)
            await state.mark_prospects_checked(c.id)

    score_stats = await score_all_prospects(state, use_ai=False, config=cfg)
    return {**stats, **score_stats}
