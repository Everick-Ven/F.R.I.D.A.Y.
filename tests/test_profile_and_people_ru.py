"""Тесты профайлера стека Рунета и извлечения русских ФИО/ЛПР для любой ниши."""

from __future__ import annotations

from friday.collectors.people import extract_people_from_html
from friday.collectors.profile import inspect_company_html
from friday.models.company import Company


def test_inspect_company_html_detects_ru_marketing_crm_and_booking_stack():
    html = """
    <html>
      <head><title>Сеть клиник «МедСфера»</title></head>
      <body>
        <script src="https://an.yandex.ru/system/context.js"></script>
        <script>VK.Retargeting.Init("VK-RTRG-123");</script>
        <script src="https://mod.calltouch.ru/init.js"></script>
        <script src="https://w123.yclients.com/widgetJS"></script>
        <link href="/bitrix/templates/med/styles.css" rel="stylesheet" />
        <a href="https://t.me/medsfera_clinic">Telegram</a>
        <p>ООО «МедСфера», ИНН 7714998877, тел. +7 (495) 900-12-34</p>
        <footer>© 2021 ООО МедСфера</footer>
      </body>
    </html>
    """
    info = inspect_company_html(
        html,
        final_url="https://medsfera-test.ru/",
        elapsed_ms=4100.0,
        had_https_redirect=False,
        current_year=2026,
    )
    codes = {o["signal_code"] for o in info["observations"]}
    assert "SLOW_SITE" in codes
    assert "NO_HTTPS_REDIRECT" in codes
    assert "NO_MOBILE_VIEWPORT" in codes
    assert "NO_YANDEX_METRIKA" in codes
    assert "YANDEX_DIRECT_ACTIVE" in codes
    assert "VK_PIXEL_ACTIVE" in codes
    assert "CALLTRACKING_ACTIVE" in codes
    assert "ONLINE_BOOKING_ACTIVE" in codes
    assert "TECH_STACK_RU" in codes
    assert "TELEGRAM_PUBLIC" in codes
    assert "OUTDATED_COPYRIGHT" in codes
    assert "THIN_SERVICE_STRUCTURE" in codes
    assert info["inn"] == "7714998877"
    assert "1С-Битрикс" in info["tech_stack"]
    assert "YCLIENTS" in info["tech_stack"]
    assert "Calltouch" in info["tech_stack"]


def test_inspect_company_html_detects_no_online_capture():
    html = "<html><head><meta name='viewport' content='width=device-width'></head><body><h1>Завод деталей</h1></body></html>"
    info = inspect_company_html(html, final_url="https://zavod.ru/", elapsed_ms=300.0)
    codes = {o["signal_code"] for o in info["observations"]}
    assert "NO_ONLINE_CAPTURE" in codes
    assert "NO_MESSENGER_WIDGET" in codes


def test_extract_people_from_html_across_russian_niches():
    comp = Company(id="c1", name="ТД СпецПоставка", industry="Дистрибуция", city="Новосибирск")
    html = """
    <html><body>
      <h2>Руководство компании</h2>
      <div class="person">
        Коммерческий директор — Соколов Артём Сергеевич.
        Прямая почта: sokolov@spec-postavka.ru, общая: info@spec-postavka.ru
      </div>
      <div class="person">
        Директор по закупкам — Захарова Наталья Игоревна.
        Почта: zakharova@spec-postavka.ru
      </div>
      <footer>Реквизиты: ИП Белов Денис Олегович</footer>
    </body></html>
    """
    people = extract_people_from_html(
        html,
        page_url="https://spec-postavka.ru/about",
        company=comp,
        custom_titles=["директор по закупкам", "коммерческий директор"],
    )
    assert len(people) == 3
    names = {p.full_name for p in people}
    assert "Соколов Артём Сергеевич" in names
    assert "Захарова Наталья Игоревна" in names
    assert "Белов Денис Олегович" in names
    # Проверяем, что не назначили общую почту info@ при наличии персональных
    emails = {p.email for p in people if p.email}
    assert "info@spec-postavka.ru" not in emails
