"""Тесты детерминированного шлюза ФЗ-38 «О рекламе» + опциональных регулируемых ниш + IDNA стоп-листа."""

from __future__ import annotations

from friday.config import FridayConfig
from friday.gate import normalize_domain_idna, validate_draft


def test_fz38_general_superlatives_and_guarantees_blocked():
    cfg = FridayConfig()
    res = validate_draft(
        channel="telegram",
        subject="",
        body="Алексей, мы лучший сервис в России и даём 100% гарантию результата за неделю. Обсудим?",
        config=cfg,
    )
    assert res.passed is False
    assert any("ФЗ-38" in r for r in res.reasons)


def test_clean_universal_b2b_draft_passes():
    cfg = FridayConfig()
    res = validate_draft(
        channel="telegram",
        subject="",
        body=(
            "Алексей Викторович, здравствуйте. Увидел открытую вакансию менеджера по продажам "
            "в «Альфа-Сталь» на hh.ru и заметил, что на сайте пока нет виджета быстрого захвата заявок. "
            "Подскажите, актуально ли прислать короткий разбор из 3 пунктов сюда в чат?"
        ),
        config=cfg,
    )
    assert res.passed is True, res.reasons


def test_regulated_niches_only_trigger_when_selected():
    body_med = "Иван Петрович, наш аппарат гарантирует излечение пациентов с первого сеанса. Прислать паспорт?"

    cfg_general = FridayConfig()
    cfg_general.compliance.regulated_niche = "none"
    # В обычной нише спецправило медицины не включено
    res_gen = validate_draft(channel="telegram", subject="", body=body_med, config=cfg_general)
    assert not any("Медицина" in r for r in res_gen.reasons)

    cfg_med = FridayConfig()
    cfg_med.compliance.regulated_niche = "medicine"
    res_med = validate_draft(channel="telegram", subject="", body=body_med, config=cfg_med)
    assert res_med.passed is False
    assert any("Медицина" in r for r in res_med.reasons)

    cfg_fin = FridayConfig()
    cfg_fin.compliance.regulated_niche = "finance"
    res_fin = validate_draft(
        channel="telegram",
        subject="",
        body="Ольга, наш инструмент гарантирует доходность 35 годовых для юрлиц. Показать расчёт?",
        config=cfg_fin,
    )
    assert res_fin.passed is False
    assert any("Финансы" in r for r in res_fin.reasons)


def test_idna_rf_and_telegram_dnc_suppression():
    cfg = FridayConfig()
    cfg.compliance.do_not_contact = ["клиент-стоп.рф", "@blocked_ceo"]

    puny = normalize_domain_idna("https://www.клиент-стоп.рф/catalog")
    res_dom = validate_draft(
        channel="email",
        subject="вопрос по поставкам",
        body="Иван, добрый день. Подскажите, актуален ли расчёт логистики?",
        company_domain=puny,
        config=cfg,
    )
    assert res_dom.passed is False
    assert any("do_not_contact" in r for r in res_dom.reasons)

    res_tg = validate_draft(
        channel="telegram",
        subject="",
        body="Иван, добрый день. Подскажите, актуален ли расчёт логистики?",
        prospect_telegram="https://t.me/blocked_ceo",
        config=cfg,
    )
    assert res_tg.passed is False
    assert any("do_not_contact" in r for r in res_tg.reasons)
