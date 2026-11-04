"""CLI интерфейс F.R.I.D.A.Y. — универсальная B2B-разведка и подготовка черновиков для любой ниши РФ.

Порядок работы:
  Шаг 1. Настройка ниши и продукта:
         `friday train https://ваш-сайт.ru --brief "..." --niche "..."`
         (или `friday setup --preset ...`, или вкладка «1. Ниша и продукт» в `friday ui`)
  Шаг 2. Сбор и профилирование компаний:
         `friday discover`, `friday import`, `friday profile`, `friday prospects`
  Шаг 3. Мозг (Вариант А — ИИ-агент рабочей среды):
         `friday score --ai`, `friday drafts --channel telegram`
         `friday tasks next` -> `friday tasks apply <id> '<json>'`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from friday.agents.scout import score_all_prospects
from friday.agents.writer import run_drafts
from friday.brain import (
    BrainError,
    apply_agent_task_result,
    enqueue_agent_task,
    load_prompt,
    load_skills,
)
from friday.collectors.discover import (
     parse_import_file,
    persist_discovered_items,
    run_discover,
)
from friday.collectors.people import run_people
from friday.collectors.profile import run_profile
from friday.config import is_niche_configured, load_config
from friday.export import export_drafts_csv, export_drafts_markdown
from friday.pipeline import bootstrap_state, run_full_pipeline, seed_sample_for_active_niche
from friday.state import StateManager
from friday.trainer import (
    NICHE_TEMPLATES_RU,
    apply_niche_setup,
    build_train_prompt_context,
    crawl_product_site,
)

console = Console()


async def _cmd_status(state: StateManager) -> int:
    await bootstrap_state(state)
    cfg = load_config()
    configured = is_niche_configured(cfg)
    summary = await state.get_state_summary()

    niche_line = (
        f"[bold green]НАСТРОЕНА[/bold green] — {cfg.persona.company} ({cfg.product.name})\n"
        f"[bold]Целевая ниша (ICP):[/bold] {cfg.icp.niche}\n"
        f"[bold]Города:[/bold] {', '.join(cfg.icp.cities) or '—'}\n"
        f"[bold]Запросы:[/bold] {', '.join(cfg.icp.search_queries[:3]) or '—'}\n"
        f"[bold]Регулируемая ниша ФЗ-38:[/bold] {cfg.compliance.regulated_niche}"
        if configured
        else (
            "[bold yellow]НЕ НАСТРОЕНА (Шаг 1)[/bold yellow]\n"
            "Сначала обучите F.R.I.D.A.Y. вашему продукту и нише:\n"
            "  • `friday train https://ваш-сайт.ru --niche \"кто ваши клиенты\"`\n"
            "  • или выберите шаблон: `friday presets` -> `friday setup --preset b2b_crm_integrator`\n"
            "  • или откройте веб-интерфейс: `friday ui`"
        )
    )

    console.print(
        Panel.fit(
            f"{niche_line}\n"
            f"[bold]Мозг (Вариант А):[/bold] ИИ-агент рабочей среды (`mode={cfg.brain.mode}`)\n"
            f"[bold]Режим комплаенса:[/bold] Только разведка + черновики (автоотправка отключена, ФЗ-38 ст. 18)",
            title="F.R.I.D.A.Y. v0.2 — Статус системы",
            border_style="cyan" if configured else "yellow",
        )
    )

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Показатель")
    table.add_column("Значение", justify="right")
    table.add_row("Компаний в базе", str(summary["companies_total"]))
    table.add_row("Ожидают профилирования сайта", str(summary["unprofiled_companies"]))
    table.add_row("ЛПР / контактов (всего)", str(summary["prospects_total"]))
    table.add_row("Подтверждённых наблюдений (сигналов)", str(summary["observations_total"]))
    table.add_row("Черновиков на проверке (pending_review)", str(summary["drafts_by_status"].get("pending_review", 0)))
    table.add_row("Черновиков одобрено (approved)", str(summary["drafts_by_status"].get("approved", 0)))
    table.add_row("Заблокировано шлюзом ФЗ-38 (gate_blocked)", str(summary["drafts_by_status"].get("gate_blocked", 0)))
    table.add_row("Ожидающих задач ИИ-агента (agent_tasks)", str(summary["pending_agent_tasks"]))
    console.print(table)
    return 0


async def _cmd_train(args: argparse.Namespace, state: StateManager) -> int:
    await bootstrap_state(state)
    cfg = load_config()
    url = (args.website or "").strip()
    brief = (args.brief or "").strip()
    niche_hint = (args.niche or "").strip()
    cities = [c.strip() for c in (args.cities or "").split(",") if c.strip()]

    if not url and not brief and not niche_hint:
        console.print(
            "[red]Укажите хотя бы URL сайта продукта или краткий бриф (`--brief` / `--niche`).[/red]"
        )
        return 1

    crawl = {"url": "", "pages": [], "error": ""}
    if url:
        console.print(f"[cyan]Скачивание страниц продукта {url} ($0, SSRF-safe)...[/cyan]")
        crawl = await crawl_product_site(url)

    sys_prompt = "\n\n".join(
        p for p in [load_prompt("system"), load_prompt("train"), load_skills(["ru_compliance"])] if p
    )
    user_prompt = build_train_prompt_context(
        website_crawl=crawl,
        user_brief=brief,
        target_niche_hint=niche_hint,
        cities_hint=cities or None,
    )
    tid, tfile = await enqueue_agent_task(
        state,
        cfg,
        kind="train_niche",
        payload={
            "website": url,
            "brief": brief,
            "niche_hint": niche_hint,
            "cities": cities,
            "pages_crawled": len(crawl.get("pages") or []),
        },
        system_prompt=sys_prompt,
        user_prompt=user_prompt,
    )
    console.print(
        Panel.fit(
            f"[green]Создана задача настройки ниши `train_niche` #{tid}[/green]\n"
            f"Пакет задачи: `{tfile}`\n\n"
            f"Чтобы ИИ-агент рабочей среды завершил настройку:\n"
            f"  1) Прочитать задачу: `friday tasks show {tid}`\n"
            f"  2) Применить JSON:   `friday tasks apply {tid} --file response.json`",
            title="Шаг 1 — Задача для ИИ-агента рабочей среды",
            border_style="green",
        )
    )
    return 0


async def _cmd_presets() -> int:
    t = Table(title="Встроенные шаблоны ниш РФ (для быстрой настройки Шага 1)")
    t.add_column("ID шаблона (`--preset`)")
    t.add_column("Сфера / Продукт")
    t.add_column("Целевая ниша (ICP)")
    for tpl in NICHE_TEMPLATES_RU:
        t.add_row(tpl["id"], tpl["label"], tpl["icp"]["niche"])
    console.print(t)
    return 0


async def _cmd_setup(args: argparse.Namespace) -> int:
    data: dict | None = None
    if args.preset:
        tpl = next((x for x in NICHE_TEMPLATES_RU if x["id"] == args.preset), None)
        if not tpl:
            console.print(f"[red]Шаблон `{args.preset}` не найден. Посмотрите `friday presets`.[/red]")
            return 1
        data = tpl
    elif args.file:
        data = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    else:
        console.print("[red]Укажите `--preset <id>` или `--file <setup.json>`.[/red]")
        return 1

    res = apply_niche_setup(data)
    console.print(f"[green]Ниша и продукт успешно сохранены:[/green] {res}")
    return 0


async def _cmd_signals(args: argparse.Namespace, state: StateManager) -> int:
    await bootstrap_state(state)
    if args.confirm:
        ok = await state.set_signal_status(args.confirm.upper(), "confirmed")
        console.print(f"Сигнал {args.confirm.upper()} -> confirmed ({ok})")
        return 0 if ok else 1
    if args.reject:
        ok = await state.set_signal_status(args.reject.upper(), "rejected")
        console.print(f"Сигнал {args.reject.upper()} -> rejected ({ok})")
        return 0 if ok else 1

    rows = await state.list_signal_codes(status=args.status)
    t = Table(title="Каталог сигналов F.R.I.D.A.Y. (универсальный рынок РФ)")
    t.add_column("Код")
    t.add_column("Статус")
    t.add_column("Категория")
    t.add_column("Название")
    t.add_column("Источник")
    for r in rows:
        st = r["status"]
        color = "green" if st == "confirmed" else ("yellow" if st == "proposed" else "red")
        t.add_row(r["code"], f"[{color}]{st}[/{color}]", r["category"], r["label"], r["collector"])
    console.print(t)
    return 0


async def _cmd_tasks(args: argparse.Namespace, state: StateManager) -> int:
    await bootstrap_state(state)
    cfg = load_config()

    if args.tasks_action == "list":
        tasks = await state.list_agent_tasks(status=args.status)
        if not tasks:
            console.print(f"[dim]Задачи со статусом `{args.status}` отсутствуют.[/dim]")
            return 0
        t = Table(title=f"Очередь задач ИИ-агента рабочей среды ({args.status})")
        t.add_column("ID")
        t.add_column("Тип (`kind`)")
        t.add_column("Статус")
        t.add_column("Детали")
        t.add_column("Создана")
        for tk in tasks:
            pl = tk.get("payload") or {}
            detail = pl.get("prospect_id") or pl.get("website") or pl.get("niche_hint") or ""
            if pl.get("channel"):
                detail = f"{detail} ({pl['channel']})"
            t.add_row(tk["id"], tk["kind"], tk["status"], str(detail), tk["created_at"])
        console.print(t)
        return 0

    if args.tasks_action == "next":
        tasks = await state.list_agent_tasks(status="pending")
        if not tasks:
            console.print("[green]Нет ожидающих задач в очереди `agent_tasks`.[/green]")
            return 0
        tk = tasks[0]
        if args.json:
            print(json.dumps(tk, ensure_ascii=False, indent=2))
        else:
            console.print(f"[bold cyan]Задача #{tk['id']} ({tk['kind']})[/bold cyan]")
            console.print(f"Применить ответ: `friday tasks apply {tk['id']} '<json>'`\n")
            print(tk["prompt_text"])
        return 0

    if args.tasks_action == "show":
        tasks = await state.list_agent_tasks(status="all")
        tk = next((x for x in tasks if x["id"] == args.task_id), None)
        if not tk:
            console.print(f"[red]Задача #{args.task_id} не найдена.[/red]")
            return 1
        print(tk["prompt_text"])
        return 0

    if args.tasks_action == "apply":
        raw_json = ""
        if args.file:
            raw_json = Path(args.file).read_text(encoding="utf-8-sig")
        elif args.response_json:
            raw_json = args.response_json
        elif not sys.stdin.isatty():
            raw_json = sys.stdin.read()
        if not raw_json.strip():
            console.print("[red]Передайте JSON-ответ строкой, через `--file` или в stdin.[/red]")
            return 1
        try:
            outcome = await apply_agent_task_result(state, args.task_id, raw_json, cfg=cfg)
            console.print(f"[green]Задача #{args.task_id} успешно применена:[/green] {outcome}")
            return 0
        except BrainError as exc:
            console.print(f"[red]Ошибка применения задачи #{args.task_id}:[/red] {exc}")
            return 1

    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="friday",
        description="F.R.I.D.A.Y. — универсальная B2B-разведка и черновики писем для любой ниши рынка РФ",
    )
    parser.add_argument("--db", default="", help="Путь к SQLite БД (по умолчанию data/friday.db)")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Показать статус настройки ниши, базы и очереди ИИ-агента")
    sub.add_parser("presets", help="Показать шаблоны ниш РФ для быстрого старта")

    p_train = sub.add_parser(
        "train",
        help="Шаг 1: обучить F.R.I.D.A.Y. продукту и нише по сайту/описанию (задача для ИИ-агента)",
    )
    p_train.add_argument("website", nargs="?", default="", help="URL сайта вашего продукта/услуги")
    p_train.add_argument("--brief", default="", help="Краткое описание вашего продукта и оффера")
    p_train.add_argument("--niche", default="", help="Кому вы хотите продавать (целевая ниша)")
    p_train.add_argument("--cities", default="", help="Города РФ через запятую")

    p_setup = sub.add_parser(
        "setup",
        help="Шаг 1: применить готовый шаблон ниши (`--preset`) или JSON-конфигурацию (`--file`)",
    )
    p_setup.add_argument("--preset", default="", help="ID шаблона из `friday presets`")
    p_setup.add_argument("--file", default="", help="Путь к JSON-файлу настройки ниши")

    p_disc = sub.add_parser("discover", help="Найти целевые компании в РФ ($0)")
    p_disc.add_argument("-q", "--query", default="", help="Поисковый запрос (по умолчанию из friday.local.yaml)")
    p_disc.add_argument("-c", "--city", default="", help="Город (по умолчанию из friday.local.yaml)")
    p_disc.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "hh_ru", "yandex_search", "twogis", "yandex_maps", "ddg_ru", "osm_ru"],
    )
    p_disc.add_argument("-n", "--limit", type=int, default=20)

    p_imp = sub.add_parser("import", help="Импортировать список компаний/контактов из CSV, JSON или TXT")
    p_imp.add_argument("file", help="Путь к файлу (.csv, .json, .jsonl, .txt)")
    p_imp.add_argument("-c", "--city", default="", help="Город по умолчанию")
    p_imp.add_argument("--industry", default="", help="Отрасль по умолчанию")

    p_prof = sub.add_parser("profile", help="Проверить сайты компаний на сигналы Рунета ($0 HTTP)")
    p_prof.add_argument("-n", "--limit", type=int, default=50)

    p_peop = sub.add_parser("prospects", help="Извлечь ФИО руководителей/ЛПР и контакты с сайтов ($0)")
    p_peop.add_argument("-n", "--limit", type=int, default=50)

    p_sig = sub.add_parser("signals", help="Управление каталогом сигналов (proposed / confirmed / rejected)")
    p_sig.add_argument("--status", choices=["proposed", "confirmed", "rejected"], default=None)
    p_sig.add_argument("--confirm", default="", help="Подтвердить код сигнала")
    p_sig.add_argument("--reject", default="", help="Отклонить код сигнала")

    p_coh = sub.add_parser("cohort", help="Выбрать когорту компаний по пересечению сигналов")
    p_coh.add_argument("--require", default="", help="Обязательные коды через запятую")
    p_coh.add_argument("--exclude", default="", help="Исключающие коды через запятую")

    p_score = sub.add_parser("score", help="Рассчитать балл соответствия нише (0..100)")
    p_score.add_argument("--ai", action="store_true", help="Поставить задачи углублённого скоринга ИИ-агенту")
    p_score.add_argument("-n", "--limit", type=int, default=100)

    p_drafts = sub.add_parser("drafts", help="Подготовить черновики писем (Вариант А: задачи ИИ-агенту)")
    p_drafts.add_argument("--channel", default="telegram", choices=["telegram", "email", "whatsapp"])
    p_drafts.add_argument("--min-score", type=int, default=None)
    p_drafts.add_argument("-n", "--limit", type=int, default=20)
    p_drafts.add_argument(
        "--template",
        action="store_true",
        help="Сразу собрать детерминированные предпросмотровые черновики без ожидания ИИ-агента",
    )

    p_tasks = sub.add_parser("tasks", help="Очередь задач для ИИ-агента рабочей среды (Вариант А)")
    tsub = p_tasks.add_subparsers(dest="tasks_action", required=True)
    tl = tsub.add_parser("list", help="Список задач ИИ-агента")
    tl.add_argument("--status", default="pending", choices=["pending", "completed", "all"])
    tn = tsub.add_parser("next", help="Показать следующую ожидающую задачу для выполнения ИИ-агентом")
    tn.add_argument("--json", action="store_true", help="Вывести JSON-пакет задачи")
    ts = tsub.add_parser("show", help="Показать промпт задачи по ID")
    ts.add_argument("task_id")
    ta = tsub.add_parser("apply", help="Применить JSON-результат от ИИ-агента рабочей среды")
    ta.add_argument("task_id")
    ta.add_argument("response_json", nargs="?", default="", help="JSON-строка ответа агента")
    ta.add_argument("--file", default="", help="Путь к JSON-файлу с ответом агента")

    p_exp = sub.add_parser("export", help="Экспортировать черновики в CSV или Markdown")
    p_exp.add_argument("--format", default="csv", choices=["csv", "md"])
    p_exp.add_argument("--status", default=None)
    p_exp.add_argument("-o", "--output", default="", help="Путь к выходному файлу")

    p_run = sub.add_parser("run", help="Запустить полный цикл разведки для настроенной ниши")
    p_run.add_argument("-q", "--query", default="")
    p_run.add_argument("-c", "--city", default="")
    p_run.add_argument("--provider", default="auto")
    p_run.add_argument("--channel", default="telegram", choices=["telegram", "email", "whatsapp"])
    p_run.add_argument("-n", "--limit", type=int, default=15)
    p_run.add_argument("--template", action="store_true")

    sub.add_parser("sample", help="Загрузить 2 тестовые карточки под текущую настроенную нишу")

    p_ui = sub.add_parser("ui", help="Запустить веб-интерфейс F.R.I.D.A.Y.")
    p_ui.add_argument("--host", default="0.0.0.0")
    p_ui.add_argument("--port", type=int, default=5555)

    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    state = StateManager(db_path=args.db or None)
    cfg = load_config()

    if args.cmd in (None, "status"):
        return await _cmd_status(state)
    if args.cmd == "presets":
        return await _cmd_presets()
    if args.cmd == "train":
        return await _cmd_train(args, state)
    if args.cmd == "setup":
        return await _cmd_setup(args)
    if args.cmd == "signals":
        return await _cmd_signals(args, state)
    if args.cmd == "tasks":
        return await _cmd_tasks(args, state)

    await bootstrap_state(state)

    if args.cmd == "discover":
        q = args.query or (cfg.icp.search_queries[0] if cfg.icp.search_queries else "")
        c = args.city or (cfg.icp.cities[0] if cfg.icp.cities else "Москва")
        if not q:
            console.print("[red]Укажите `-q <запрос>` или сначала настройте нишу (`friday train`).[/red]")
            return 1
        res = await run_discover(state, query=q, city=c, provider=args.provider, limit=args.limit, config=cfg)
        console.print(f"[green]Поиск завершён:[/green] {res}")
        return 0

    if args.cmd == "import":
        parsed = parse_import_file(
            args.file,
            default_city=args.city or (cfg.icp.cities[0] if cfg.icp.cities else ""),
            default_industry=args.industry or cfg.icp.niche,
        )
        res = await persist_discovered_items(state, parsed, config=cfg, run_id="cli_import")
        console.print(f"[green]Импорт завершён:[/green] {res}")
        return 0

    if args.cmd == "profile":
        res = await run_profile(state, limit=args.limit)
        console.print(f"[green]Профилирование завершено:[/green] {res}")
        return 0

    if args.cmd == "prospects":
        res = await run_people(state, limit=args.limit, config=cfg)
        console.print(f"[green]Поиск ЛПР завершён:[/green] {res}")
        return 0

    if args.cmd == "cohort":
        req = [x.strip().upper() for x in args.require.split(",") if x.strip()]
        exc = [x.strip().upper() for x in args.exclude.split(",") if x.strip()]
        companies = await state.cohort(require=req, exclude=exc)
        t = Table(title=f"Когорта компаний ({len(companies)})")
        t.add_column("Название")
        t.add_column("Отрасль")
        t.add_column("Город")
        t.add_column("Домен")
        for c in companies:
            t.add_row(c.name, c.industry, c.city, c.domain)
        console.print(t)
        return 0

    if args.cmd == "score":
        res = await score_all_prospects(state, use_ai=args.ai, limit=args.limit, config=cfg)
        console.print(f"[green]Скоринг завершён:[/green] {res}")
        return 0

    if args.cmd == "drafts":
        mode_ov = "template" if args.template else None
        res = await run_drafts(
            state,
            channel=args.channel,
            min_score=args.min_score,
            limit=args.limit,
            mode_override=mode_ov,
            config=cfg,
        )
        console.print(f"[green]Подготовка черновиков:[/green] {res}")
        return 0

    if args.cmd == "export":
        content = (
            await export_drafts_csv(state, status=args.status)
            if args.format == "csv"
            else await export_drafts_markdown(state, status=args.status)
        )
        if args.output:
            Path(args.output).write_text(content, encoding="utf-8")
            console.print(f"[green]Экспортировано в {args.output}[/green]")
        else:
            sys.stdout.write(content)
        return 0

    if args.cmd == "run":
        res = await run_full_pipeline(
            state,
            query=args.query or None,
            city=args.city or None,
            provider=args.provider,
            limit=args.limit,
            channel=args.channel,
            mode_override="template" if args.template else None,
            config=cfg,
        )
        console.print(f"[green]Конвейер завершён:[/green] {res}")
        return 0

    if args.cmd == "sample":
        res = await seed_sample_for_active_niche(state, config=cfg)
        console.print(f"[green]Тестовые карточки под текущую нишу добавлены:[/green] {res}")
        return 0

    return 1


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "ui":
        import uvicorn
        from friday.dashboard import create_app

        app = create_app(db_path=args.db or None)
        uvicorn.run(app, host=args.host, port=args.port)
        return

    code = asyncio.run(_dispatch(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
