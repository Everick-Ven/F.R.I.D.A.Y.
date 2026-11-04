"""Тесты универсального поиска компаний в РФ (hh.ru, Яндекс Search, 2ГИС, импорт CSV с ИНН/отраслью)."""

from __future__ import annotations

import base64
from pathlib import Path
import httpx
import pytest
import respx

from friday.collectors.discover import (
    discover_hh_ru,
    discover_twogis,
    discover_yandex_search,
    is_junk_ru_domain,
    parse_import_file,
    persist_discovered_items,
)
from friday.state import StateManager


def test_junk_ru_aggregators_filtered_across_niches():
    assert is_junk_ru_domain("https://www.avito.ru/moskva/oborudovanie") is True
    assert is_junk_ru_domain("https://ozon.ru/seller/123") is True
    assert is_junk_ru_domain("https://www.rusprofile.ru/id/12345") is True
    assert is_junk_ru_domain("https://hh.ru/vacancy/999") is True
    assert is_junk_ru_domain("https://2gis.ru/moscow") is True
    # Реальные сайты бизнеса пропускаются
    assert is_junk_ru_domain("https://metall-konstrukcii.ru") is False
    assert is_junk_ru_domain("https://пекарня-хлеб.рф") is False


@pytest.mark.asyncio
@respx.mock
async def test_discover_hh_ru_free_keyless_api():
    respx.get("https://api.hh.ru/vacancies").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "Менеджер по продажам B2B",
                        "alternate_url": "https://hh.ru/vacancy/777111",
                        "area": {"name": "Екатеринбург"},
                        "employer": {
                            "id": "55501",
                            "name": "ООО УралСпецСталь",
                            "alternate_url": "https://hh.ru/employer/55501",
                        },
                    }
                ]
            },
        )
    )
    respx.get("https://api.hh.ru/employers/55501").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "55501",
                "name": "ООО УралСпецСталь",
                "site_url": "https://ural-spec-stal.ru",
                "open_vacancies": 4,
                "industries": [{"name": "Металлообработка"}],
                "description": "<p>Производство и поставка металлоконструкций по УрФО.</p>",
            },
        )
    )

    items = await discover_hh_ru("металлоконструкции", "Екатеринбург", hiring_role="менеджер по продажам", limit=5)
    assert len(items) == 1
    it = items[0]
    assert it.name == "ООО УралСпецСталь"
    assert it.website == "https://ural-spec-stal.ru"
    assert it.industry == "Металлообработка"
    codes = {o["signal_code"] for o in it.observations}
    assert "HH_HIRING_ROLE" in codes
    assert "HH_OPEN_ROLES" in codes


@pytest.mark.asyncio
@respx.mock
async def test_discover_yandex_search_page2_and_twogis(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "test-ya-key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "test-folder")
    monkeypatch.setenv("TWOGIS_API_KEY", "test-2gis-key")

    xml_page0 = "<yandexsearch><response><results><grouping></grouping></results></response></yandexsearch>"
    xml_page1 = """
    <yandexsearch><response><results><grouping>
      <group><doc><url>https://avito.ru/skip</url><title>Авито</title></doc></group>
      <group><doc><url>https://klinika-ulybka.ru/</url><title>Стоматология Улыбка</title></doc></group>
    </grouping></results></response></yandexsearch>
    """
    calls = {"n": 0}

    def _ya_handler(_request):
        idx = calls["n"]
        calls["n"] += 1
        raw = xml_page1 if idx == 1 else xml_page0
        b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return httpx.Response(200, json={"rawData": b64})

    respx.post("https://searchapi.api.cloud.yandex.net/v2/web/search").mock(side_effect=_ya_handler)
    ya_items = await discover_yandex_search("стоматологическая клиника", "Москва", limit=10)
    assert len(ya_items) == 1
    assert ya_items[0].observations[0]["signal_code"] == "YANDEX_PAGE2_RANK"

    respx.get("https://catalog.api.2gis.com/3.0/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "items": [
                        {
                            "id": "70000001_1",
                            "name": "АвтоТехЦентр Юг",
                            "address_name": "ул. Северная, 15",
                            "rubrics": [{"name": "Автосервис"}],
                            "reviews": {"general_rating": 3.8, "general_review_count": 29},
                            "contact_groups": [
                                {"contacts": [{"type": "phone", "value": "+7 (861) 200-10-20"}]}
                            ],
                        }
                    ]
                }
            },
        )
    )
    gis_items = await discover_twogis("кузовной ремонт", "Краснодар", limit=5)
    assert len(gis_items) == 1
    gis_codes = {o["signal_code"] for o in gis_items[0].observations}
    assert "LOW_MAP_RATING" in gis_codes
    assert "NO_WEBSITE_ON_MAP" in gis_codes


@pytest.mark.asyncio
async def test_universal_csv_import_with_inn_and_fio(state: StateManager, tmp_path: Path):
    csv_file = tmp_path / "kontur_export.csv"
    csv_file.write_text(
        "Компания;ИНН;Отрасль;Сайт;Город;ФИО;Должность;Email;Telegram\n"
        "ООО ЛогистТранс;7725112233;Логистика и ВЭД;logist-trans.ru;Москва;Орлов Максим Игоревич;Директор по логистике;orlov@logist-trans.ru;https://t.me/logist_trans\n",
        encoding="utf-8",
    )
    parsed = parse_import_file(csv_file)
    stats = await persist_discovered_items(state, parsed)
    assert stats["inserted_companies"] == 1
    assert stats["inserted_prospects"] == 1

    companies = await state.get_companies()
    assert companies[0].inn == "7725112233"
    assert companies[0].industry == "Логистика и ВЭД"

    prospects = await state.get_prospects()
    assert prospects[0].polite_greeting == "Максим Игоревич"
    assert prospects[0].title == "Директор по логистике"
