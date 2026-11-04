# F.R.I.D.A.Y. — Руководство для Claude Code

F.R.I.D.A.Y. — система разведки и подготовки персонального ручного аутрича для бюро юридического трафика (привлечение доверителей для адвокатов, адвокатских бюро и коллегий в России в рамках **ФЗ-38 «О рекламе»** и **ст. 17 КПЭА**).

Когда пользователь открывает этот проект в Claude Code, помогите ему быстро проверить готовность окружения и настроить работу под его задачи.

## Быстрая проверка состояния
1. Установлен ли пакет `friday` в виртуальном окружении (`.venv`)? Если нет — `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`.
2. Создан ли `friday.local.yaml` с реальными данными бюро пользователя? Если нет — предложите скопировать `cp friday.yaml friday.local.yaml` и заполнить города, специализации и описание бюро.
3. Подтверждены ли сигналы сбора? Выполните `friday signals --confirm free` (или покажите веб-панель `friday dashboard`).

## Режимы работы с Claude
- **По подписке Claude CLI (`brain.provider: claude` в `friday.yaml`)**: как и в оригинальном Harvey, `friday` вызывает `claude -p` локально в изолированном режиме без инструментов (`--tools ""`, `--safe-mode`, `--no-session-persistence`), расходуя лимит вашей подписки без оплаты за API-токены.
- **Прямо внутри диалога Claude Code (`brain.provider: agent`)**: вы можете сами выполнять задачи скоринга и написания черновиков через `friday tasks list` → `friday tasks show <id>` → `friday tasks apply <id>`.

## Ключевые команды
```bash
friday status                                    # Сводка по пайплайну
friday signals --confirm free                    # Подтвердить бесплатные сигналы
friday discover --providers                      # Статус источников (Яндекс, 2ГИС, Карты, OSM)
friday import leads.csv                          # Импорт своего списка практик
friday import minjust_moscow.csv --registry      # Импорт выгрузки реестра адвокатов региона
friday profile                                   # Разбор сайтов (Метрика, Директ, КПЭА, ФИО ЛПР)
friday scout                                     # Оценка практик 1–100 и формулировка сигнала
friday draft                                     # Генерация досье и черновиков + проверка Gate
friday export --excel-ru --dossiers dossiers.md  # Выгрузка CSV для Excel и рабочей тетради .md
friday dashboard                                 # Веб-интерфейс на http://127.0.0.1:5555
```
