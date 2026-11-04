"""Интеграционные тесты FastAPI сервера F.R.I.D.A.Y. v0.2 (Шаг 1 «Ниша и продукт» + конвейер + экспорт)."""

from __future__ import annotations

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

        # 4. Постановка задач черновиков ИИ-агенту рабочей среды (Вариант А)
        q_res = client.post("/api/run/drafts", json={"channel": "telegram", "template_preview": False})
        assert q_res.status_code == 200
        assert q_res.json()["queued_agent_tasks"] >= 1

        tasks = client.get("/api/agent-tasks?status=pending").json()["items"]
        assert len(tasks) >= 1

        # 5. И быстрый предпросмотр шаблонных черновиков + экспорт CSV
        tpl_res = client.post("/api/run/drafts", json={"channel": "telegram", "template_preview": True})
        assert tpl_res.status_code == 200
        assert tpl_res.json()["drafts_ready"] >= 1

        csv_r = client.get("/api/export/csv")
        assert csv_r.status_code == 200
        assert "Логистика" in csv_r.text or "Дистрибуция" in csv_r.text
