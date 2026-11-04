"""Тесты источников российской разведки: Яндекс XML, 2ГИС, Яндекс Карты, реестры Минюста и CSV в CP1251."""

import pytest

from friday.collectors.discover import (
    DiscoveryQuery,
    import_practices_from_file,
    is_junk_ru,
    parse_twogis_items,
    parse_yandex_maps_features,
    parse_yandex_xml,
    persist_discovered_practices,
)
from friday.config import FridayConfig
from friday.state import StateManager


def test_junk_filter_russian_aggregators_and_courts():
    assert is_junk_ru("pravoved.ru", "Правовед") is True
    assert is_junk_ru("msk.zoon.ru", "Юристы Москвы") is True
    assert is_junk_ru("kad.arbitr.ru", "Картотека арбитражных дел") is True
    assert is_junk_ru("advokatymoscow.ru", "Адвокатская палата") is True
    assert is_junk_ru("example-rating.ru", "Топ-15 лучших адвокатов по уголовным делам") is True
    assert is_junk_ru("ab-volkov.example", "Адвокатское бюро «Волков и партнёры»") is False


def test_parse_yandex_xml_ranks():
    sample_xml = """<?xml version="1.0" encoding="utf-8"?>
    <yandexsearch version="1.0">
      <response>
        <results>
          <grouping>
            <group><doc><url>https://zoon.ru/msk/law/</url><title>Рейтинг юристов</title></doc></group>
            <group><doc><url>https://kollegia-arbitr.example/uslugi</url><title>МКА «Арбитр Плюс» | Арбитражные споры в Москве</title></doc></group>
          </grouping>
        </results>
      </response>
    </yandexsearch>
    """
    q = DiscoveryQuery(term="адвокат по арбитражным спорам", city="Москва")
    items = parse_yandex_xml(sample_xml, q)
    assert len(items) == 2
    assert items[1].domain == "kollegia-arbitr.example"
    assert items[1].yandex_rank == 2
    assert items[1].legal_form == "Коллегия адвокатов"


def test_parse_twogis_and_yandex_maps():
    q = DiscoveryQuery(term="коллегия адвокатов", city="Екатеринбург")
    twogis_items = parse_twogis_items(
        [
            {
                "id": "70000001012345_01",
                "name": "Адвокатское бюро «УралПраво»",
                "address_name": "ул. Малышева, 51",
                "reviews": {"general_rating": 4.9, "general_review_count": 7},
                "contact_groups": [
                    {
                        "contacts": [
                            {"type": "phone", "value": "+7 (343) 000-11-22"},
                            {"type": "telegram", "value": "@uralpravo_example"},
                        ]
                    }
                ],
                "rubrics": [{"name": "Адвокаты"}],
            }
        ],
        query=q,
    )
    assert len(twogis_items) == 1
    p = twogis_items[0]
    assert p.has_website is False
    assert p.card_incomplete is True
    assert p.rating == 4.9
    assert p.telegram == "@uralpravo_example"

    ymaps_items = parse_yandex_maps_features(
        [
            {
                "properties": {
                    "CompanyMetaData": {
                        "id": "1122334455",
                        "name": "Коллегия адвокатов «Защита»",
                        "url": "https://zaschita-ekb.example",
                        "address": "Екатеринбург, пр. Ленина, 20",
                        "Phones": [{"formatted": "+7 (343) 222-33-44"}],
                        "Categories": [{"name": "Коллегия адвокатов"}],
                        "Hours": {"text": "пн-пт 09:00-19:00"},
                    }
                }
            }
        ],
        query=q,
    )
    assert len(ymaps_items) == 1
    assert ymaps_items[0].domain == "zaschita-ekb.example"
    assert ymaps_items[0].card_incomplete is False


@pytest.mark.asyncio
async def test_import_regional_registry_and_cp1251_csv(state_db: StateManager, tmp_path):
    await state_db.set_signal_status("YANDEX_SERP_RANK", "confirmed")
    cfg = FridayConfig()

    # 1. HTML-таблица регионального реестра адвокатов (проверка фильтрации прекращённого статуса)
    reg_html = """
    <table>
      <tr><th>ФИО</th><th>Реестровый номер</th><th>Статус</th><th>Адвокатское образование</th></tr>
      <tr><td>Соколов Дмитрий Андреевич</td><td>77/99101</td><td>Действующий</td><td>МКА «Соколов и партнёры»</td></tr>
      <tr><td>Петров Игорь Олегович</td><td>77/99102</td><td>Прекращён</td><td>Адвокатский кабинет</td></tr>
    </table>
    """
    reg_file = tmp_path / "registry.html"
    reg_file.write_text(reg_html, encoding="utf-8")

    rep_reg = await import_practices_from_file(
        state_db, reg_file, config=cfg, is_registry=True, default_city="Москва"
    )
    assert rep_reg.new_companies == 1
    assert rep_reg.new_prospects == 1
    assert rep_reg.skipped_junk == 1

    # 2. CSV в кодировке Windows-1251 с разделителем `;` (типичная выгрузка из русского Excel)
    csv_cp1251 = (
        "название;фио;город;сайт;telegram\r\n"
        "АБ «Казань Арбитраж»;Морозова Елена Викторовна;Казань;https://kazan-arb.example;@morozova_law_ex\r\n"
    ).encode("cp1251")
    csv_file = tmp_path / "leads_ru.csv"
    csv_file.write_bytes(csv_cp1251)

    rep_csv = await import_practices_from_file(state_db, csv_file, config=cfg)
    assert rep_csv.new_companies == 1
    assert rep_csv.new_prospects == 1

    prospects = await state_db.get_prospects()
    names = {p.full_name() for p in prospects}
    assert "Соколов Дмитрий Андреевич" in names
    assert "Морозова Елена Викторовна" in names
