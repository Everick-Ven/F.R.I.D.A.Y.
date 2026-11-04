"""Тесты детерминированного комплаенс-фильтра Gate: ФЗ-38 «О рекламе», ст. 17 КПЭА, анти-спам и стоп-лист."""

from friday.config import ComplianceConfig, FridayConfig
from friday.gate import check_draft, match_do_not_contact


def test_gate_passes_clean_consultative_message():
    cfg = FridayConfig()
    body = (
        "Алексей Сергеевич, здравствуйте.\n\n"
        "Посмотрел выдачу Яндекса по арбитражным спорам в Москве: сайт вашего бюро на 15-й позиции, "
        "при этом корпоративные споры и банкротство сведены на одну общую страницу.\n\n"
        "Мы профильно занимаемся привлечением доверителей для адвокатских бюро по правилам ст. 17 КПЭА "
        "и ФЗ «О рекламе», все кабинеты оформляются на клиента. Подскажите, задача по загрузке практики "
        "на этот квартал актуальна?"
    )
    res = check_draft(channel="telegram", subject="", body=body, config=cfg)
    assert res.ok is True
    assert res.reasons == []


def test_gate_blocks_fz38_guarantees_and_superlatives():
    cfg = FridayConfig()
    bad_body = (
        "Здравствуйте. Мы лучшее агентство на рынке и гарантируем поток заявок "
        "и 100% результат уже в первый месяц. Обсудим?"
    )
    res = check_draft(channel="telegram", subject="", body=bad_body, config=cfg)
    assert res.ok is False
    assert any("ФЗ-38" in r for r in res.reasons)


def test_gate_blocks_kpea_article_17_violations():
    cfg = FridayConfig()
    bad_body = (
        "Алексей Сергеевич, здравствуйте. Давайте накрутим отзывы в Яндекс Картах и разместим отзывы "
        "доверителей на вашем сайте, чтобы обойти других адвокатов?"
    )
    res = check_draft(channel="telegram", subject="", body=bad_body, config=cfg)
    assert res.ok is False
    assert any("КПЭА" in r for r in res.reasons)


def test_gate_blocks_russian_ai_cliches_and_fake_reply():
    cfg = FridayConfig()
    bad_body = (
        "Доброго времени суток! В современном мире важно вывести практику на новый уровень "
        "и получить взрывной рост. Созвонимся?"
    )
    res = check_draft(channel="email", subject="Re: сотрудничество", body=bad_body, config=cfg)
    assert res.ok is False
    assert any("Re:" in r for r in res.reasons)
    assert any("восклицательн" in r.lower() for r in res.reasons)
    assert any("доброго времени суток" in r.lower() for r in res.reasons)


def test_do_not_contact_supports_rf_domains_and_telegram():
    cfg = FridayConfig(
        compliance=ComplianceConfig(
            do_not_contact=[
                "https://коллегия-стоп.рф/about",
                "https://t.me/blocked_partner",
                "vip@client.example",
            ]
        )
    )
    dnc = cfg.compliance.do_not_contact
    assert match_do_not_contact(domain="sub.xn----8sbfg0a0be2a2f.xn--p1ai", entries=dnc) == ""
    assert match_do_not_contact(domain="коллегия-стоп.рф", entries=dnc) != ""
    assert match_do_not_contact(telegram="@blocked_partner", entries=dnc) == "@blocked_partner"
    assert match_do_not_contact(email="vip+tag@client.example", entries=dnc) == "vip@client.example"
