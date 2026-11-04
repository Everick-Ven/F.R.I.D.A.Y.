"""Тесты профилирования российских юридических сайтов и извлечения русских ФИО / должностей."""

from friday.collectors.people import extract_people_from_html, split_russian_fio
from friday.collectors.profile import analyze_practice_site_html


def test_split_russian_fio_orders_and_non_names():
    # Фамилия Имя Отчество
    assert split_russian_fio("Волков Алексей Сергеевич") == ("Волков", "Алексей", "Сергеевич")
    # Имя Отчество Фамилия
    assert split_russian_fio("Елена Викторовна Морозова") == ("Морозова", "Елена", "Викторовна")
    # 2 слова
    assert split_russian_fio("Алексей Волков") == ("Волков", "Алексей", "")

    # Заголовки разделов сайта НЕ должны превращаться в людей
    assert split_russian_fio("Уголовные Дела Москва") is None
    assert split_russian_fio("Коллегия Адвокатов Защита") is None
    assert split_russian_fio("Арбитражные Споры") is None


def test_extract_people_and_analyze_russian_legal_site():
    sample_html = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Адвокатское бюро «Волков и партнёры» — Москва</title>
      <script src="https://tildacdn.com/js/tilda-blocks.js"></script>
      <script src="https://mod.calltouch.ru/init.js"></script>
    </head>
    <body>
      <nav>
        <a href="/uslugi">Все услуги</a>
        <a href="/komanda">Команда</a>
        <a href="/kontakty">Контакты</a>
      </nav>
      <section>
        <h1>Защита по экономическим делам — 100% гарантия результата</h1>
        <div class="team-card">
          <h3>Волков Алексей Сергеевич</h3>
          <p>Управляющий партнёр, адвокат. Реестровый номер № 77/98765 в реестре адвокатов г. Москвы.</p>
        </div>
        <div class="team-card">
          <h3>Орлова Марина Игоревна</h3>
          <p>Руководитель практики банкротства</p>
        </div>
      </section>
      <footer>
        <a href="mailto:office@volkov-law.example">office@volkov-law.example</a>
        <a href="https://t.me/volkov_bureau_example">Telegram</a>
        <p>Телефон: +7 (495) 000-12-34. Разработка сайта — Студия ПравоВеб</p>
      </footer>
    </body>
    </html>
    """
    people = extract_people_from_html(sample_html, page_url="https://volkov-law.example/komanda")
    assert len(people) == 2
    assert people[0]["last_name"] == "Волков"
    assert people[0]["first_name"] == "Алексей"
    assert people[0]["middle_name"] == "Сергеевич"
    assert people[0]["role_kind"] == "managing_partner"
    assert people[0]["registry_number"] == "77/98765"

    res = analyze_practice_site_html(sample_html, base_url="https://volkov-law.example")
    assert res["legal_form"] == "Адвокатское бюро"
    assert "Tilda" in res["tech_stack"]
    assert "Calltouch" in res["tech_stack"]
    assert res["has_metrika"] is False
    assert "Calltouch" in res["paid_tools"]
    assert res["missing_practice_pages"] is True
    assert len(res["compliance_risks"]) >= 1
    assert res["telegram"] == "@volkov_bureau_example"
    assert res["public_email"] == "office@volkov-law.example"
    assert res["incumbent_agency"] == "Студия ПравоВеб"
