"""Интеграционные тесты FastAPI сервера F.R.I.D.A.Y. v0.2 (Шаг 1 «Ниша и продукт» + конвейер + экспорт)."""

from __future__ import annotations

import json
from pathlib import Path
from fastapi.testclient import TestClient

from friday.dashboard import create_app


def test_dashboard_starts_clean_and_supports_niche_presets_and_pipeline(
    tmp_path: Path,
    monkeypatch,
):
    # Изолируем friday.local.yaml и skills во временной папке, чтобы не менять рабочее дерево
    monkeypatch.setattr("friday.trainer.LOCAL_CONFIG_PATH", tmp_path / "friday.local.yaml")
    monkeypatch.setattr("friday.trainer.SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr("friday.dashboard.SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(
        "friday.config.find_config_file",
        lambda explicit_path=None: (
            Path(explicit_path)
            if explicit_path
            else (
                tmp_path / "friday.local.yaml"
                if (tmp_path / "friday.local.yaml").exists()
                else Path(__file__).resolve().parent.parent / "friday.yaml"
            )
        ),
    )

    app = create_app(db_path=str(tmp_path / "ui_test.db"))
    with TestClient(app) as client:
        # 1. Изначально ниша НЕ настроена и база пуста (нет хардкода юристов!)
        r = client.get("/api/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["niche_configured"] is False
        assert body["summary"]["companies_total"] == 0

        # 2. Запрашиваем шаблоны ниш РФ и сохраняем выбранную нишу (Шаг 1)
        presets = client.get("/api/niche/presets").json()["items"]
        assert len(presets) >= 4
        logistics_preset = next(p for p in presets if p["id"] == "b2b_logistics_ved")

        save_res = client.post("/api/niche/save", json=logistics_preset)
        assert save_res.status_code == 200
        assert save_res.json()["niche_configured"] is True

        # 3. Загружаем 2 тестовые карточки под выбранную нишу логистики и ВЭД
        sample_res = client.post("/api/run/sample")
        assert sample_res.status_code == 200
        assert sample_res.json()["inserted_companies"] == 2

        # 4. Постановка задач черновиков ИИ-агенту рабочей среды (без шаблонов)
        q_res = client.post("/api/run/drafts", json={"channel": "telegram"})
        assert q_res.status_code == 200
        assert q_res.json()["queued_agent_tasks"] >= 1

        tasks = client.get("/api/agent-tasks?status=pending").json()["items"]
        assert len(tasks) >= 1
        writer_task_id = tasks[0]["id"]
        assert tasks[0]["kind_label"] == "Подготовка персонального предложения"
        assert "prompt_text" not in tasks[0]

        # ИИ-агент применяет индивидуальное письмо (без шаблона), проверяем выгрузку CSV
        comp_res = client.post(
            f"/api/agent-tasks/{writer_task_id}/complete",
            json={
                "response_json": json.dumps(
                    {
                        "subject": "",
                        "body": "Алексей Викторович, здравствуйте. Увидел открытую вакансию в вашей компании на hh.ru, при этом на сайте пока нет быстрой формы для входящих запросов. Подскажите, кто сейчас разбирает обращения новых клиентов?",
                        "audit_summary": "Открыт подбор в отдел продаж, на сайте нет онлайн-захвата.",
                        "signal_used": "Поиск профильного сотрудника (hh.ru)",
                    },
                    ensure_ascii=False,
                )
            },
        )
        assert comp_res.status_code == 200
        assert comp_res.json()["outcome"]["gate_passed"] is True

        if len(tasks) >= 2:
            del_r = client.delete(f"/api/agent-tasks/{tasks[1]['id']}")
            assert del_r.status_code == 200
            assert del_r.json()["status"] == "cancelled"

        # Проверяем отсутствие кэширования, заглушек «Демо-данные» и технических промптов
        root_resp = client.get("/")
        js_resp = client.get("/static/app.js")
        assert "no-store" in root_resp.headers.get("cache-control", "")
        assert "no-store" in js_resp.headers.get("cache-control", "")
        html_txt = root_resp.text
        js_txt = js_resp.text
        assert "Демо-данные" not in html_txt + js_txt
        assert "btn-sample" not in html_txt + js_txt
        assert "btn-open-brain" not in html_txt + js_txt
        assert "brain-modal" not in html_txt + js_txt
        assert "Настройки Мозга" not in html_txt + js_txt
        assert "Скопировать полный промпт задачи" not in html_txt + js_txt
        assert "Применить JSON-ответ" not in html_txt + js_txt
        assert "Очередь задач ИИ-агента" not in html_txt + js_txt
        assert "ФЗ-38" not in html_txt
        assert "SQLite" not in html_txt

        csv_r = client.get("/api/export/csv")
        assert csv_r.status_code == 200
        assert "Логистика" in csv_r.text or "Дистрибуция" in csv_r.text


def test_live_progress_jobs_train_and_drafts_for_legal_seo_niche(
    tmp_path: Path,
    monkeypatch,
):
    import time

    monkeypatch.setattr("friday.trainer.LOCAL_CONFIG_PATH", tmp_path / "friday.local.yaml")
    monkeypatch.setattr("friday.trainer.SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr("friday.dashboard.SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(
        "friday.config.find_config_file",
        lambda explicit_path=None: (
            Path(explicit_path)
            if explicit_path
            else (
                tmp_path / "friday.local.yaml"
                if (tmp_path / "friday.local.yaml").exists()
                else Path(__file__).resolve().parent.parent / "friday.yaml"
            )
        ),
    )

    async def _fast_crawl(url: str, *, timeout: float = 4.5) -> dict:
        return {"url": url, "pages": [], "error": ""}

    monkeypatch.setattr("friday.dashboard.crawl_product_site", _fast_crawl)

    app = create_app(db_path=str(tmp_path / "live_jobs.db"))
    with TestClient(app) as client:
        start = client.post(
            "/api/jobs/train",
            json={
                "website": "https://pravotraffic.ru",
                "niche_hint": "Адвокаты и адвокатские бюро",
                "brief": (
                    "Разрабатываем сайты для адвокатов, настраиваем и ведем SEO, определяем приоритетные "
                    "направления развития, предлагаем инструменты, распределяем задачи и формируем "
                    "последовательный план работ с учётом целей, бюджета и особенностей адвокатской практики."
                ),
                "cities": ["Москва", "Санкт-Петербург", "Екатеринбург", "Казань"],
            },
        )
        assert start.status_code == 200
        job = start.json()
        job_id = job["id"]

        for _ in range(40):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] != "running":
                break
            time.sleep(0.05)

        assert job["status"] == "completed"
        assert job["percent"] == 100
        assert job["result"]["niche_configured"] is True

        summary = client.get("/api/summary").json()
        assert summary["niche_configured"] is True
        assert summary["config"]["icp"]["niche"] == "Адвокаты и адвокатские бюро"
        assert summary["config"]["compliance"]["regulated_niche"] == "legal_advocates"

        # Импорт живых карточек в двух разных городах (Ростов-на-Дону и Москва) + фильтр города
        batch_res = client.post(
            "/api/import/live-batch",
            json={
                "items": [
                    {
                        "name": "Адвокатское бюро Грата Интернешнл Ростов-на-Дону",
                        "website": "https://rostovadvocat.ru",
                        "city": "Ростов-на-Дону",
                        "address": "Ростов-на-Дону, пр. Михаила Нагибина, 28",
                        "phone": "+7 (863) 245-08-28",
                        "source": "web_ru",
                    },
                    {
                        "name": "Адвокатское бюро Премиум",
                        "ymaps_url": "https://yandex.ru/maps/org/premium/1166100050984/",
                        "city": "Ростов-на-Дону",
                        "address": "Ростов-на-Дону, Социалистическая ул., 74",
                        "phone": "+7 (863) 200-11-22",
                        "inn": "6165200323",
                        "source": "yandex_maps",
                    },
                    {
                        "name": "МКА Князев и партнеры",
                        "website": "https://kniazev.ru",
                        "city": "Москва",
                        "phone": "+7 (495) 987-18-70",
                        "source": "web_ru",
                    },
                ]
            },
        )
        assert batch_res.status_code == 200
        assert batch_res.json()["inserted_companies"] == 3

        # Строгая фильтрация по городу Ростов-на-Дону (без московских карточек!)
        rostov_comps = client.get("/api/companies?city=Ростов-на-Дону").json()["items"]
        assert len(rostov_comps) == 2
        assert all(c["city"] == "Ростов-на-Дону" for c in rostov_comps)

        # Живой аудит страницы компании + извлечение ФИО ЛПР
        rostov_web_comp = next(c for c in rostov_comps if c["website"])
        enrich_res = client.post(
            "/api/enrich/live-pages",
            json={
                "pages": [
                    {
                        "company_id": rostov_web_comp["id"],
                        "url": "https://rostovadvocat.ru",
                        "title": "Адвокатское бюро в Ростове-на-Дону",
                        "content": (
                            "![Logo](https://rostovadvocat.ru/wp-content/uploads/logo.png)\n"
                            "Управляющий партнёр Сердюков Константин Владимирович\n"
                            "Телефон: +7 (863) 245-08-28, почта: partner@rostovadvocat.ru, ИНН 6164012345"
                        ),
                    }
                ]
            },
        )
        assert enrich_res.status_code == 200
        assert enrich_res.json()["profiled"] == 1
        assert enrich_res.json()["prospects_added"] >= 1

        # Проверяем, что в заметках ЛПР и названиях сигналов нет сырых английских кодов вроде DECISION_MAKER_FOUND
        pros_items = client.get("/api/prospects?city=Ростов-на-Дону").json()["items"]
        assert all("DECISION_MAKER_FOUND" not in (p.get("personalization_notes") or "") for p in pros_items)
        assert all("NO_ONLINE_CAPTURE" not in (p.get("personalization_notes") or "") for p in pros_items)
        comp_items = client.get("/api/companies?city=Ростов-на-Дону").json()["items"]
        for c in comp_items:
            for obs in c.get("observations") or []:
                assert obs.get("signal_label")
                assert "DECISION_MAKER_FOUND" not in obs["signal_label"]

        # Проверяем, что БЕЗ КАКИХ-ЛИБО РУЧНЫХ НАСТРОЕК МОЗГА кнопка «Подготовить предложение» работает с 1 клика!
        zero_cfg_prep = client.post(
            "/api/proposals/prepare",
            json={"company_id": rostov_web_comp["id"], "channel": "telegram"},
        )
        assert zero_cfg_prep.status_code == 200
        zero_draft = zero_cfg_prep.json()["draft"]
        assert zero_draft is not None
        assert "Константин Владимирович" in zero_draft["body"]
        assert "—" not in zero_draft["body"]

        # А при наличии ключа в окружении система САМА выбирает лучшую модель и Humanizer без участия пользователя
        async def _mock_harvey_llm(self, prompt: str, *, timeout: float = 90.0, **kwargs) -> str:
            if "draft_to_humanize" in prompt:
                return json.dumps(
                    {
                        "subject": "",
                        "body": (
                            "Константин Владимирович, здравствуйте. Посмотрел rostovadvocat.ru на WordPress, "
                            "разделы практик есть, но быстрой записи для доверителей не увидел. "
                            "Обращения с поиска сейчас принимаете только по телефону?"
                        ),
                    },
                    ensure_ascii=False,
                )
            if "Константин Владимирович" in prompt:
                body = (
                    "Константин Владимирович, здравствуйте. Посмотрел сайт rostovadvocat.ru на WordPress, "
                    "страницы практик есть, но быстрой формы записи для доверителей не увидел. "
                    "Подскажите, обращения с поиска сейчас принимаете только по телефону?"
                )
            else:
                body = (
                    "Здравствуйте. Нашёл карточку «Адвокатское бюро Премиум» в Яндекс.Картах по Ростову-на-Дону, "
                    "при этом отдельный сайт бюро там не привязан. "
                    "Подскажите, планируете ли в этом году выводить практику в органический поиск Яндекса?"
                )
            return json.dumps(
                {
                    "subject": "",
                    "body": body,
                    "audit_summary": "Проверены открытые карточки и сайт бюро в Ростове-на-Дону.",
                    "signal_used": "Отсутствие формы захвата на сайте",
                },
                ensure_ascii=False,
            )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-auto-selected-key")
        monkeypatch.setattr("friday.brain.Brain._call_http_llm", _mock_harvey_llm)

        prep_res = client.post(
            "/api/proposals/prepare",
            json={"company_id": rostov_web_comp["id"], "channel": "telegram"},
        )
        assert prep_res.status_code == 200
        draft_obj = prep_res.json()["draft"]
        assert draft_obj is not None
        assert draft_obj["brain_provider"] == "api:claude-3-5-sonnet-latest"
        assert "Константин Владимирович" in draft_obj["body"]

        # Сохранение замечания владельца (Feedback, как в Harvey)
        fb_res = client.post(
            "/api/feedback",
            json={
                "comment": "Не упоминать адрес офиса в первом сообщении.",
                "about": "Адвокатское бюро Грата",
                "prospect_id": draft_obj["prospect_id"],
                "draft_id": draft_obj["id"],
            },
        )
        assert fb_res.status_code == 200

        edit_ok = client.patch(
            f"/api/drafts/{draft_obj['id']}/content",
            json={
                "subject": "",
                "body": "Константин Владимирович, здравствуйте. Изучил сайт rostovadvocat.ru. Подскажите, актуально ли прислать короткий разбор из 3 конкретных пунктов?",
            },
        )
        assert edit_ok.status_code == 200
        assert edit_ok.json()["gate_passed"] is True

        # Пакетная подготовка персональных писем через Мозг только по г. Ростов-на-Дону
        dr = client.post(
            "/api/jobs/drafts",
            json={"channel": "telegram", "city": "Ростов-на-Дону", "limit": 10},
        ).json()
        for _ in range(40):
            dr = client.get(f"/api/jobs/{dr['id']}").json()
            if dr["status"] != "running":
                break
            time.sleep(0.05)
        assert dr["status"] == "completed"
        assert dr["result"]["drafts_ready"] >= 2

        drafts = client.get("/api/drafts?city=Ростов-на-Дону").json()["items"]
        assert len(drafts) >= 2
        assert all(d["brain_provider"] != "template" for d in drafts)
        assert all("Не задано" not in d["body"] for d in drafts)
        assert all(d["status"] == "pending_review" for d in drafts)


def test_discover_job_succeeds_even_when_external_tls_is_blocked(
    tmp_path: Path,
    monkeypatch,
):
    import httpx
    import time

    monkeypatch.setattr("friday.trainer.LOCAL_CONFIG_PATH", tmp_path / "friday.local.yaml")
    monkeypatch.setattr("friday.trainer.SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr("friday.dashboard.SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(
        "friday.config.find_config_file",
        lambda explicit_path=None: (
            Path(explicit_path)
            if explicit_path
            else (
                tmp_path / "friday.local.yaml"
                if (tmp_path / "friday.local.yaml").exists()
                else Path(__file__).resolve().parent.parent / "friday.yaml"
            )
        ),
    )

    async def _tls_eof(*_args, **_kwargs):
        raise httpx.ConnectError("TLS/SSL connection has been closed (EOF) (_ssl.c:992)")

    monkeypatch.setattr("friday.collectors.discover.discover_hh_ru", _tls_eof)
    monkeypatch.setattr("friday.collectors.discover.discover_ddg_ru", _tls_eof)
    monkeypatch.setattr("friday.collectors.discover.discover_osm_ru", _tls_eof)

    app = create_app(db_path=str(tmp_path / "disc_tls.db"))
    with TestClient(app) as client:
        client.post(
            "/api/niche/train",
            json={
                "website": "https://pravotraffic.ru",
                "niche_hint": "Адвокаты и адвокатские бюро",
                "brief": "Разрабатываем сайты для адвокатов и ведем SEO.",
                "cities": ["Москва", "Санкт-Петербург"],
            },
        )

        disc = client.post(
            "/api/jobs/discover",
            json={"query": "адвокатское бюро", "city": "Москва", "provider": "auto", "limit": 8},
        ).json()

        for _ in range(50):
            disc = client.get(f"/api/jobs/{disc['id']}").json()
            if disc["status"] != "running":
                break
            time.sleep(0.05)

        assert disc["status"] == "completed"
        assert disc["percent"] == 100
        assert disc["result"]["network_isolated"] is True
        assert disc["result"]["inserted_companies"] == 0
        assert "_ssl.c" not in disc["stage"]

        # Браузерный ретранслятор передаёт живые организации с реальными сайтами
        batch = client.post(
            "/api/import/live-batch",
            json={
                "items": [
                    {
                        "name": "МКА «Адвокаты и бизнес»",
                        "website": "https://buroaib.ru",
                        "city": "Москва",
                        "industry": "Адвокатские бюро",
                        "address": "ул. Пятницкая, д. 49а, стр. 2",
                        "phone": "+7 (499) 230-37-28",
                        "email": "office@buroaib.ru",
                        "source": "hh_ru",
                        "external_id": "hh:real_1",
                        "observations": [
                            {
                                "signal_code": "HH_OPEN_ROLES",
                                "collector": "hh_ru",
                                "value_num": 2,
                                "value_text": "2 открытые вакансии на hh.ru",
                                "evidence_url": "https://buroaib.ru",
                            }
                        ],
                    },
                    {
                        "name": "МКА «Князев и партнеры»",
                        "website": "https://www.kniazev.ru",
                        "city": "Москва",
                        "industry": "Адвокатские бюро",
                        "address": "ул. Малая Дмитровка, д. 16/6",
                        "phone": "+7 (495) 987-18-70",
                        "email": "kniazev@kniazev.ru",
                        "contact_fio": "Князев Андрей Геннадьевич",
                        "contact_title": "Председатель коллегии",
                        "source": "hh_ru",
                        "external_id": "hh:real_2",
                    },
                ]
            },
        )
        assert batch.status_code == 200
        assert batch.json()["inserted_companies"] == 2
        assert batch.json()["scored"] >= 2

        companies = client.get("/api/companies").json()["items"]
        prospects = client.get("/api/prospects").json()["items"]
        assert len(companies) == 2
        assert len(prospects) == 2


