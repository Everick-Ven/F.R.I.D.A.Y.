# CLAUDE.md — Руководство для ИИ-агента рабочей среды

Подробный протокол работы описан в файле [`AGENTS.md`](./AGENTS.md).

## Быстрая шпаргалка

- **Шаг 1 (Настройка любой ниши РФ)**:
  - `.venv/bin/friday status` — проверить, настроена ли ниша (`friday.local.yaml`).
  - `.venv/bin/friday train <url> --niche "..." --brief "..."` — скачать сайт продукта и создать задачу `train_niche`.
  - `.venv/bin/friday presets` / `.venv/bin/friday setup --preset <id>` — быстрые шаблоны ниш РФ.
- **Сбор и профилирование ($0, без LLM)**:
  - `.venv/bin/friday discover --provider hh_ru -n 15` (бесплатный API HeadHunter без ключей!)
  - `.venv/bin/friday profile`
  - `.venv/bin/friday prospects`
  - `.venv/bin/friday score`
- **Мозг (Вариант А — ИИ-агент рабочей среды)**:
  - `.venv/bin/friday drafts --channel telegram`
  - `.venv/bin/friday tasks next`
  - `.venv/bin/friday tasks apply <task_id> '<json>'`
- **Тесты и веб-интерфейс**:
  - `.venv/bin/pytest -v`
  - `.venv/bin/friday ui --host 0.0.0.0 --port 5555`
