"""Тесты веб-панели F.R.I.D.A.Y. (FastAPI маршруты обзора, импорта, подготовки черновиков и выгрузки)."""

from fastapi.testclient import TestClient

from friday.dashboard import create_app


def test_dashboard_end_to_end_flow(tmp_path):
    db_file = tmp_path / "dash.db"
    app = create_app(db_path=db_file)

    with TestClient(app) as client:
        r_idx = client.get("/")
        assert r_idx.status_code == 200
        assert "F.R.I.D.A.Y." in r_idx.text

        # 1. Подтверждаем бесплатные сигналы
        r_sig = client.post("/api/signals/bulk", json={"mode": "confirm_free"})
        assert r_sig.status_code == 200
        assert r_sig.json()["updated"] >= 15

        # 2. Импортируем тестовую карточку коллегии
        csv_body = (
            "название;фио;реестровый номер;статус;город;сайт;telegram\n"
            "КА «Невский Арбитраж»;Орлов Сергей Николаевич;78/10500;действующий;Санкт-Петербург;;@orlov_spb_ex\n"
        )
        r_imp = client.post(
            "/api/import",
            json={"content": csv_body, "is_registry": True, "default_city": "Санкт-Петербург"},
        )
        assert r_imp.status_code == 200
        assert r_imp.json()["new_companies"] == 1

        # 3. Запускаем подготовку в детерминированном режиме `template`
        r_prep = client.post(
            "/api/pipeline/prepare",
            json={"min_score": 20, "limit": 10, "provider_override": "template"},
        )
        assert r_prep.status_code == 200
        assert r_prep.json()["writer"]["drafts_ready"] >= 1

        # 4. Проверяем реестр и черновики
        r_comp = client.get("/api/companies")
        assert r_comp.status_code == 200
        assert len(r_comp.json()["companies"]) == 1

        r_drafts = client.get("/api/drafts")
        assert r_drafts.status_code == 200
        drafts = r_drafts.json()["drafts"]
        assert len(drafts) >= 1
        assert "Сергей Николаевич" in drafts[0]["body"]

        # 5. Проверяем скачивание CSV и тетради досье
        r_csv = client.get("/api/export/prospects.csv")
        assert r_csv.status_code == 200
        assert "КА «Невский Арбитраж»" in r_csv.text

        r_md = client.get("/api/export/dossiers.md")
        assert r_md.status_code == 200
        assert "Рабочая тетрадь ручного аутрича" in r_md.text
