"""CLI-интерфейс F.R.I.D.A.Y. — команды разведки, профилирования, оценки, черновиков и экспорта."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from friday.agents.scout import Scout
from friday.agents.writer import Writer
from friday.brain import Brain
from friday.collectors.discover import (
     DEFAULT_PROVIDER,
    PROVIDERS,
    DiscoveryQuery,
    import_practices_from_file,
    run_discovery,
)
from friday.config import ConfigError, FridayConfig, load_config, load_env
from friday.export import export_dossiers_markdown, export_prospects_csv
from friday.pipeline import apply_agent_task_result, run_full_pipeline, run_profile_stage
from friday.register import build_company_register, register_to_csv
from friday.signals import ALL_SIGNAL_CODES, FREE_SIGNAL_CODES, seed_signal_catalog
from friday.state import StateManager
from friday.textio import read_user_text, utf8_stdio


async def _boot(auto_confirm_free: bool = False) -> tuple[StateManager, FridayConfig]:
    state = StateManager()
    await state.init_db()
    await seed_signal_catalog(state, auto_confirm_free=auto_confirm_free)
    try:
        cfg = load_config()
    except Exception:
        cfg = FridayConfig()
    return state, cfg


def cmd_status(_args) -> None:
    async def _run():
        state, cfg = await _boot()
        env = load_env()
        brain = Brain(state, brain_config=cfg.brain, env=env)
        ready, msg = await brain.preflight()
        s = await state.get_state_summary()

        print("\n  F.R.I.D.A.Y. — Статус пайплайна разведки")
        print("  " + "=" * 48)
        print(f"  Бюро:                      {cfg.persona.company}")
        print(f"  Режим работы:              Разведка + черновики (без автоотправки, ст. 18 ФЗ-38)")
        print(f"  Движок анализа (Brain):    {cfg.brain.provider} ({'готов' if ready else 'требует настройки'})")
        print(f"    └─ {msg}")
        print(f"  Практик в реестре:         {s['companies_total']} (ждут разбора сайта: {s['unprofiled_companies']})")
        print(f"  Контактов адвокатов / ЛПР: {s['prospects_total']} {s['prospects_by_status']}")
        print(f"  Сигналов подтверждено:     {s['signals_by_status'].get('confirmed', 0)} (наблюдений в БД: {s['observations_total']})")
        print(f"  Черновиков всего:          {s['drafts_total']} {s['drafts_by_status']}")
        if s["pending_agent_tasks"]:
            print(f"  Ожидают ответа ИИ-агента:  {s['pending_agent_tasks']} (`friday tasks list`)")
        print()

    asyncio.run(_run())


def cmd_signals(args) -> None:
    async def _run():
        state, _ = await _boot()
        if args.confirm:
            raw = args.confirm.strip()
            if raw.lower() == "free":
                targets = sorted(FREE_SIGNAL_CODES)
            elif raw.lower() == "all":
                targets = sorted(ALL_SIGNAL_CODES)
            else:
                targets = [x.strip().upper() for x in raw.split(",") if x.strip()]
            cnt = 0
            for code in targets:
                if await state.set_signal_status(code, "confirmed"):
                    cnt += 1
            print(f"\n  ✓ Подтверждено сигналов: {cnt}\n")
            return

        if args.reject:
            targets = [x.strip().upper() for x in args.reject.split(",") if x.strip()]
            cnt = 0
            for code in targets:
                if await state.set_signal_status(code, "rejected"):
                    cnt += 1
            print(f"\n  ✓ Отклонено сигналов: {cnt}\n")
            return

        rows = await state.list_signal_codes()
        print("\n  Словарь сигналов F.R.I.D.A.Y. (собираются только `confirmed`):")
        print("  " + "-" * 72)
        for r in rows:
            mark = "✓" if r["status"] == "confirmed" else ("✗" if r["status"] == "rejected" else "·")
            print(f"  [{mark}] {r['code']:<28} ({r['status']:<9}) — {r['label']}")
        print("\n  Подсказка: `friday signals --confirm free` подтвердит все бесплатные сигналы.\n")

    asyncio.run(_run())


def cmd_discover(args) -> None:
    async def _run():
        state, cfg = await _boot()
        env = load_env()
        if args.providers:
            print("\n  Доступные провайдеры разведки (DISCOVER):")
            print("  " + "-" * 72)
            for p in PROVIDERS.values():
                ok = "готов" if p.configured(env) else f"нужны ключи: {', '.join(p.missing_keys(env))}"
                print(f"  • {p.key:<16} — {p.label} [{ok}]")
                print(f"    {p.blurb}")
            print()
            return

        queries = None
        if args.query:
            queries = [DiscoveryQuery(term=args.query, city=args.city or (cfg.icp.cities[0] if cfg.icp.cities else ""))]

        rep = await run_discovery(
            state,
            cfg,
            env,
            provider_key=args.provider or DEFAULT_PROVIDER,
            queries=queries,
            max_spend_rub=args.max_spend,
            dry_run=args.estimate,
        )
        if args.estimate:
            print(f"\n  Оценка стоимости ({rep.provider}): {rep.estimated_cost_rub:.2f} ₽\n")
            return
        print(
            f"\n  Разведка завершена ({rep.provider}): запросов {rep.queries_run}, "
            f"новых практик {rep.new_companies}, обновлено {rep.updated_companies}, "
            f"наблюдений {rep.observations_written}, отсеяно агрегаторов {rep.skipped_junk}.\n"
        )
        for err in rep.errors:
            print(f"  ! {err}")

    asyncio.run(_run())


def cmd_import(args) -> None:
    async def _run():
        state, cfg = await _boot(auto_confirm_free=True)
        path = Path(args.file)
        if not path.exists():
            print(f"  Файл не найден: {path}", file=sys.stderr)
            sys.exit(2)
        rep = await import_practices_from_file(
            state,
            path,
            config=cfg,
            is_registry=args.registry,
            default_city=args.city or "",
        )
        print(
            f"\n  ✓ Импорт из `{path.name}` завершён:\n"
            f"    Новых практик: {rep.new_companies} (обновлено: {rep.updated_companies})\n"
            f"    Именных контактов адвокатов: {rep.new_prospects}\n"
            f"    Записано сигналов: {rep.observations_written}\n"
            f"    Отсеяно (агрегаторы / недейств. статус / стоп-лист): {rep.skipped_junk + rep.skipped_dnc}\n"
        )

    asyncio.run(_run())


def cmd_profile(args) -> None:
    async def _run():
        state, cfg = await _boot(auto_confirm_free=True)
        comp_cnt, obs_cnt, prosp_cnt, _ = await run_profile_stage(
            state, config=cfg, limit=args.limit
        )
        print(
            f"\n  ✓ Профилирование завершено: проверено сайтов — {comp_cnt}, "
            f"записано наблюдений — {obs_cnt}, найдено контактов ЛПР — {prosp_cnt}.\n"
        )

    asyncio.run(_run())


def cmd_scout(args) -> None:
    async def _run():
        state, cfg = await _boot()
        if args.provider:
            cfg.brain.provider = args.provider
        brain = Brain(state, brain_config=cfg.brain, env=load_env())
        scout = Scout(brain=brain, state=state, config=cfg)
        res = await scout.score_pending_prospects(limit=args.limit, rescore_all=args.rescore)
        print(f"\n  ✓ Скоринг Scout ({res.get('mode', cfg.brain.provider)}): оценено {res['scored']}.")
        if res["queued_task_ids"]:
            print(f"    Поставлено задач для ИИ-агента: {', '.join(res['queued_task_ids'])} (`friday tasks list`)")
        print()

    asyncio.run(_run())


def cmd_draft(args) -> None:
    async def _run():
        state, cfg = await _boot()
        if args.provider:
            cfg.brain.provider = args.provider
        channels = [c.strip() for c in args.channels.split(",") if c.strip()] if args.channels else None
        brain = Brain(state, brain_config=cfg.brain, env=load_env())
        writer = Writer(brain=brain, state=state, config=cfg)
        res = await writer.generate_drafts(
            min_score=args.min_score, limit=args.limit, channels=channels
        )
        print(
            f"\n  ✓ Подготовка черновиков ({res['mode']}): "
            f"готовы к проверке — {res['drafts_ready']}, "
            f"остановлены фильтром ФЗ-38/КПЭА — {res['drafts_blocked_by_gate']}."
        )
        if res["queued_task_ids"]:
            print(f"    Поставлено задач для ИИ-агента: {', '.join(res['queued_task_ids'])} (`friday tasks list`)")
        print()

    asyncio.run(_run())


def cmd_run(args) -> None:
    async def _run():
        state, cfg = await _boot(auto_confirm_free=True)
        if args.provider:
            cfg.brain.provider = args.provider
        rep = await run_full_pipeline(
            state,
            cfg,
            load_env(),
            discover_provider=args.discover_provider,
            skip_discover=args.skip_discover,
        )
        print("\n  ✓ Полный цикл F.R.I.D.A.Y. завершён:")
        print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2))
        print()

    asyncio.run(_run())


def cmd_drafts(args) -> None:
    async def _run():
        state, _ = await _boot()
        if args.approve:
            await state.set_draft_status(args.approve, "approved")
            print(f"  ✓ Черновик {args.approve} одобрен к ручной отправке.")
            return
        if args.sent:
            await state.set_draft_status(args.sent, "sent_manually")
            print(f"  ✓ Черновик {args.sent} отмечен как отправленный вручную.")
            return
        if args.reject:
            await state.set_draft_status(args.reject, "rejected")
            print(f"  ✓ Черновик {args.reject} отклонён.")
            return

        rows = await state.list_drafts(status=args.status, limit=50)
        if not rows:
            print("\n  Черновиков пока нет. Запустите `friday draft`.\n")
            return
        for d in rows:
            fio = " ".join(x for x in (d["last_name"], d["first_name"], d["middle_name"]) if x) or "Общий контакт"
            print(f"\n  [{d['id']}] {d['company_name']} ({d['city']}) — {fio} | {d['channel'].upper()} | {d['status']}")
            if d["subject"]:
                print(f"  Тема: {d['subject']}")
            print("  " + "-" * 60)
            for ln in d["body"].splitlines():
                print(f"  {ln}")
        print()

    asyncio.run(_run())


def cmd_companies(args) -> None:
    async def _run():
        state, cfg = await _boot()
        rows = await build_company_register(state, cfg)
        if args.out:
            Path(args.out).write_text(register_to_csv(rows), encoding="utf-8-sig")
            print(f"\n  ✓ Реестр практик ({len(rows)} строк) выгружен в {args.out}\n")
            return
        print(f"\n  Реестр адвокатских образований и практик ({len(rows)}):")
        print("  " + "-" * 76)
        for r in rows[:40]:
            print(f"  [{r['fit']:>3}] {r['company']} ({r['city'] or 'РФ'}) — {r['status']} | {r['signals']}")
        print()

    asyncio.run(_run())


def cmd_export(args) -> None:
    async def _run():
        state, cfg = await _boot()
        out_csv = args.out or "prospects.csv"
        cnt, _ = await export_prospects_csv(
            state,
            cfg,
            out_path=out_csv,
            min_score=args.min_score,
            include_all=args.all,
            excel_ru=args.excel_ru,
        )
        print(f"\n  ✓ Выгружено контактов в `{out_csv}`: {cnt}")
        if args.dossiers:
            md_cnt, _ = await export_dossiers_markdown(
                state, cfg, out_path=args.dossiers, min_score=args.min_score
            )
            print(f"  ✓ Рабочая тетрадь досье сохранена в `{args.dossiers}` ({md_cnt} карточек)")
        print()

    asyncio.run(_run())


def cmd_tasks(args) -> None:
    async def _run():
        state, cfg = await _boot()
        if args.subcmd == "list":
            tasks = await state.list_agent_tasks(status=args.status)
            if not tasks:
                print("\n  Очередь задач для ИИ-агента пуста.\n")
                return
            for t in tasks:
                print(f"  • {t['id']} | {t['kind']:<14} | {t['status']} | создана: {t['created_at']}")
            return

        if args.subcmd == "show":
            tasks = await state.list_agent_tasks(status="all")
            target = next((t for t in tasks if t["id"] == args.task_id), None)
            if not target:
                print(f"  Задача {args.task_id} не найдена.", file=sys.stderr)
                sys.exit(2)
            print(target["prompt_text"])
            return

        if args.subcmd == "apply":
            if args.file:
                raw = read_user_text(args.file)
            else:
                raw = sys.stdin.read()
            res = await apply_agent_task_result(state, cfg, args.task_id, raw)
            print(f"\n  ✓ Ответ ИИ-агента применён: {json.dumps(res, ensure_ascii=False)}\n")

    asyncio.run(_run())


def cmd_dashboard(args) -> None:
    from friday.dashboard import start_dashboard

    start_dashboard(host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="friday",
        description="F.R.I.D.A.Y. — ИИ-разведка и подготовка ручного аутрича для юридического рынка РФ",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Сводка по базе практик, сигналам, черновикам и Brain")

    p_sig = sub.add_parser("signals", help="Просмотр и подтверждение словаря сигналов")
    p_sig.add_argument("--confirm", help="Подтвердить сигналы: `free`, `all` или список кодов через запятую")
    p_sig.add_argument("--reject", help="Отклонить сигналы (коды через запятую)")

    p_disc = sub.add_parser("discover", help="Поиск практик (Яндекс, 2ГИС, Яндекс Карты, OSM РФ)")
    p_disc.add_argument("--providers", action="store_true", help="Показать доступные источники и статус ключей")
    p_disc.add_argument("--estimate", action="store_true", help="Оценить стоимость запуска без запросов")
    p_disc.add_argument("--provider", help="Ключ провайдера: yandex_search, twogis, yandex_maps, duckduckgo_ru, osm_ru")
    p_disc.add_argument("--query", help="Конкретная поисковая тема (например: 'адвокат по банкротству')")
    p_disc.add_argument("--city", help="Город поиска (например: 'Екатеринбург')")
    p_disc.add_argument("--max-spend", type=float, default=None, help="Лимит бюджета в рублях")

    p_imp = sub.add_parser("import", help="Импортировать собственный список (CSV/JSON/TXT) или реестр адвокатов")
    p_imp.add_argument("file", help="Путь к файлу списка или выгрузки реестра")
    p_imp.add_argument("--registry", action="store_true", help="Режим реестра адвокатов Минюста/палаты")
    p_imp.add_argument("--city", default="", help="Город по умолчанию, если его нет в колонках")

    p_prof = sub.add_parser("profile", help="Профилировать сайты практик ($0, без LLM)")
    p_prof.add_argument("--limit", type=int, default=50, help="Максимум сайтов за проход")

    p_scout = sub.add_parser("scout", help="Оценить контакты (1–100) и сформулировать угол персонализации")
    p_scout.add_argument("--limit", type=int, default=30)
    p_scout.add_argument("--provider", choices=["claude", "agent", "openai_compatible", "template"])
    p_scout.add_argument("--rescore", action="store_true", help="Пересчитать уже оценённые контакты")

    p_draft = sub.add_parser("draft", help="Подготовить досье и черновики с проверкой по 38-ФЗ и ст. 17 КПЭА")
    p_draft.add_argument("--limit", type=int, default=10)
    p_draft.add_argument("--min-score", type=int, default=45)
    p_draft.add_argument("--channels", help="Каналы через запятую (например: telegram,email)")
    p_draft.add_argument("--provider", choices=["claude", "agent", "openai_compatible", "template"])

    p_run = sub.add_parser("run", help="Запустить полный цикл: разведка -> профиль -> скоринг -> черновики")
    p_run.add_argument("--provider", choices=["claude", "agent", "openai_compatible", "template"])
    p_run.add_argument("--discover-provider", default=None)
    p_run.add_argument("--skip-discover", action="store_true", help="Работать только по уже загруженной базе")

    p_dr = sub.add_parser("drafts", help="Просмотр и одобрение готовых черновиков")
    p_dr.add_argument("--status", default=None)
    p_dr.add_argument("--approve", help="Одобрить черновик по ID")
    p_dr.add_argument("--sent", help="Отметить черновик как отправленный вручную по ID")
    p_dr.add_argument("--reject", help="Отклонить черновик по ID")

    p_comp = sub.add_parser("companies", help="Показать или выгрузить реестр практик с Fit-оценкой")
    p_comp.add_argument("--out", help="Путь для сохранения CSV-реестра")

    p_exp = sub.add_parser("export", help="Выгрузить базу контактов и черновиков в CSV и Markdown-досье")
    p_exp.add_argument("--out", default="prospects.csv", help="Путь к CSV-файлу")
    p_exp.add_argument("--dossiers", default="", help="Путь к Markdown-тетради досье (например: dossiers.md)")
    p_exp.add_argument("--min-score", type=int, default=0)
    p_exp.add_argument("--excel-ru", action="store_true", help="Разделитель `;` и UTF-8 BOM для русского Excel")
    p_exp.add_argument("--all", action="store_true", help="Включить все записи без фильтров")

    p_tasks = sub.add_parser("tasks", help="Управление задачами для ИИ-агента рабочей среды (`brain.provider: agent`)")
    tasks_sub = p_tasks.add_subparsers(dest="subcmd", required=True)
    p_tl = tasks_sub.add_parser("list", help="Список задач в очереди")
    p_tl.add_argument("--status", default="pending")
    p_ts = tasks_sub.add_parser("show", help="Вывести промпт задачи")
    p_ts.add_argument("task_id")
    p_ta = tasks_sub.add_parser("apply", help="Применить JSON-ответ от ИИ-агента")
    p_ta.add_argument("task_id")
    p_ta.add_argument("--file", help="Путь к JSON-файлу с ответом (иначе читается stdin)")

    p_dash = sub.add_parser("dashboard", help="Запустить локальную веб-панель управления")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=5555)

    return parser


def main(argv: list[str] | None = None) -> None:
    utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "status": cmd_status,
        "signals": cmd_signals,
        "discover": cmd_discover,
        "import": cmd_import,
        "profile": cmd_profile,
        "scout": cmd_scout,
        "draft": cmd_draft,
        "run": cmd_run,
        "drafts": cmd_drafts,
        "companies": cmd_companies,
        "export": cmd_export,
        "tasks": cmd_tasks,
        "dashboard": cmd_dashboard,
    }
    handler = dispatch.get(args.command)
    if not handler:
        parser.print_help()
        return
    try:
        handler(args)
    except ConfigError as exc:
        print(f"\n  Ошибка конфигурации: {exc}\n", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
