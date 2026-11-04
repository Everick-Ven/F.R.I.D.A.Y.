"""FastAPI веб-сервер F.R.I.D.A.Y. (0.0.0.0:5555, поддержка прокси Live Preview, относительные `/api/*`)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
import tempfile

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from friday.agents.scout import score_all_prospects
from friday.agents.writer import generate_draft_for_prospect, run_drafts
from friday.brain import (
    BrainError,
    apply_agent_task_result,
    enqueue_agent_task,
    load_prompt,
    load_skills,
)
from friday.collectors.discover import parse_import_file, persist_discovered_items, run_discover
from friday.collectors.people import run_people
from friday.collectors.profile import run_profile
from friday.config import is_niche_configured, load_config
from friday.export import export_drafts_csv, export_drafts_markdown
from friday.jobs import JobManager, JobRecord
from friday.paths import SKILLS_DIR
from friday.pipeline import bootstrap_state, seed_sample_for_active_niche
from friday.state import StateManager
from friday.trainer import (
    NICHE_TEMPLATES_RU,
    apply_niche_setup,
    build_train_prompt_context,
    crawl_product_site,
    synthesize_niche_profile,
)

WEB_DIR = Path(__file__).resolve().parent / "web"


class NicheSaveRequest(BaseModel):
    persona: dict = Field(default_factory=dict)
    product: dict = Field(default_factory=dict)
    icp: dict = Field(default_factory=dict)
    compliance: dict = Field(default_factory=dict)
    product_knowledge_md: str = ""
    competitive_intel_md: str = ""


class NicheTrainRequest(BaseModel):
    website: str = ""
    brief: str = ""
    niche_hint: str = ""
    cities: list[str] = Field(default_factory=list)


class DiscoverRequest(BaseModel):
    query: str = ""
    city: str = ""
    provider: str = "auto"
    limit: int = Field(default=15, ge=1, le=50)


class DraftsRequest(BaseModel):
    channel: str = "telegram"
    min_score: int | None = None
    limit: int = Field(default=20, ge=1, le=100)
    template_preview: bool = False


class SingleDraftRequest(BaseModel):
    prospect_id: str
    channel: str = "telegram"
    template_preview: bool = False


class StatusPatch(BaseModel):
    status: str


class AgentTaskCompletion(BaseModel):
    response_json: str


def create_app(db_path: str | None = None) -> FastAPI:
    state = StateManager(db_path=db_path)
    jobs = JobManager()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        await bootstrap_state(state, auto_confirm_core=True)
        yield

    app = FastAPI(
        title="F.R.I.D.A.Y.",
        version="0.3.0",
        lifespan=_lifespan,
    )

    @app.middleware("http")
    async def _preview_headers(request: Request, call_next):
        resp = await call_next(request)
        if "X-Frame-Options" in resp.headers:
            del resp.headers["X-Frame-Options"]
        return resp

    @app.get("/api/health")
    async def api_health():
        cfg = load_config()
        return {"ok": True, "version": "0.3.0", "niche_configured": is_niche_configured(cfg)}

    @app.get("/api/summary")
    async def api_summary():
        cfg = load_config()
        summary = await state.get_state_summary()
        prod_skill = SKILLS_DIR / "product_knowledge.md"
        comp_skill = SKILLS_DIR / "competitive_intel.md"
        return {
            "summary": summary,
            "niche_configured": is_niche_configured(cfg),
            "config": cfg.model_dump(),
            "active_jobs": jobs.active_jobs(),
            "skills": {
                "product_knowledge_md": prod_skill.read_text(encoding="utf-8") if prod_skill.exists() else "",
                "competitive_intel_md": comp_skill.read_text(encoding="utf-8") if comp_skill.exists() else "",
            },
        }

    # ── Фоновые операции с прогрессом в реальном времени ──

    @app.get("/api/jobs")
    async def api_list_jobs():
        return {"items": jobs.active_jobs()}

    @app.get("/api/jobs/{job_id}")
    async def api_get_job(job_id: str):
        rec = jobs.get(job_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Операция не найдена")
        return rec.as_dict()

    @app.post("/api/jobs/train")
    async def api_job_train(req: NicheTrainRequest):
        if not req.website.strip() and not req.brief.strip() and not req.niche_hint.strip():
            raise HTTPException(
                status_code=400,
                detail="Укажите адрес сайта или краткое описание вашего проекта и аудитории",
            )

        async def _run_train(job: JobRecord) -> dict:
            job.set_step(0, percent=15, detail="Анализ сайта и исходных материалов")
            await asyncio.sleep(0.15)
            crawl = (
                await crawl_product_site(req.website, timeout=4.5)
                if req.website.strip()
                else {"url": "", "pages": [], "error": ""}
            )

            job.set_step(1, percent=52, detail="Формирование портрета клиентов и поисковых запросов")
            await asyncio.sleep(0.2)
            profile_data = synthesize_niche_profile(
                website=req.website,
                crawl=crawl,
                brief=req.brief,
                niche_hint=req.niche_hint,
                cities=req.cities,
            )

            job.set_step(2, percent=82, detail="Настройка критериев отбора и параметров тона")
            await asyncio.sleep(0.2)
            cfg = load_config()
            saved = apply_niche_setup(profile_data, base_config=cfg)

            job.set_step(3, percent=96, detail="Сохранение профиля проекта")
            await asyncio.sleep(0.1)
            new_cfg = load_config()
            job.complete(
                {
                    "saved": saved,
                    "pages_crawled": len(crawl.get("pages") or []),
                    "niche_configured": is_niche_configured(new_cfg),
                    "company": new_cfg.persona.company,
                    "niche": new_cfg.icp.niche,
                },
                summary_stage=f"Профиль «{new_cfg.persona.company}» сохранён",
            )
            return job.result

        rec = jobs.start(
            kind="train",
            title="Настройка проекта",
            step_labels=[
                "Анализ сайта и исходных материалов",
                "Формирование портрета клиентов и запросов",
                "Настройка критериев отбора",
                "Сохранение профиля проекта",
            ],
            runner=_run_train,
        )
        return rec.as_dict()

    @app.post("/api/jobs/discover")
    async def api_job_discover(req: DiscoverRequest):
        cfg = load_config()
        q = req.query.strip() or (cfg.icp.search_queries[0] if cfg.icp.search_queries else "")
        c = req.city.strip() or (cfg.icp.cities[0] if cfg.icp.cities else "Москва")
        if not q:
            raise HTTPException(
                status_code=400,
                detail="Укажите поисковый запрос или настройте профиль проекта",
            )

        async def _run_disc(job: JobRecord) -> dict:
            job.set_step(0, percent=20, detail=f"Поиск компаний по запросу «{q}» ({c})")
            disc = await run_discover(
                state,
                query=q,
                city=c,
                provider=req.provider,
                limit=req.limit,
                config=cfg,
            )
            job.set_step(1, percent=65, detail="Проверка сайтов и извлечение контактов ЛПР")
            prof = await run_profile(state, limit=max(10, req.limit))
            peop = await run_people(state, limit=max(10, req.limit), config=cfg)

            job.set_step(2, percent=90, detail="Оценка релевантности найденных карточек")
            scor = await score_all_prospects(state, use_ai=False, limit=200, config=cfg)
            res = {
                "inserted_companies": disc.get("inserted_companies", 0),
                "profiled": prof.get("profiled", 0),
                "prospects_saved": peop.get("prospects_saved", 0),
                "scored": scor.get("scored", 0),
            }
            job.complete(
                res,
                summary_stage=f"Найдено компаний: {res['inserted_companies']} · ЛПР: {res['prospects_saved']}",
            )
            return res

        rec = jobs.start(
            kind="discover",
            title="Поиск и обогащение компаний",
            step_labels=[
                f"Поиск по запросу «{q}»",
                "Анализ сайтов и контактов ЛПР",
                "Оценка релевантности",
            ],
            runner=_run_disc,
        )
        return rec.as_dict()

    @app.post("/api/jobs/enrich")
    async def api_job_enrich():
        cfg = load_config()

        async def _run_enrich(job: JobRecord) -> dict:
            job.set_step(0, percent=25, detail="Анализ сайтов и маркетинговых сигналов")
            prof = await run_profile(state, limit=40)
            job.set_step(1, percent=65, detail="Поиск руководителей и прямых контактов")
            peop = await run_people(state, limit=40, config=cfg)
            job.set_step(2, percent=90, detail="Расчёт оценки соответствия профилю")
            scor = await score_all_prospects(state, use_ai=False, limit=200, config=cfg)
            res = {
                "profiled": prof.get("profiled", 0),
                "prospects_saved": peop.get("prospects_saved", 0),
                "scored": scor.get("scored", 0),
            }
            job.complete(
                res,
                summary_stage=f"Проверено сайтов: {res['profiled']} · Контактов ЛПР: {res['scored']}",
            )
            return res

        rec = jobs.start(
            kind="enrich",
            title="Анализ сайтов и поиск ЛПР",
            step_labels=[
                "Анализ сайтов и сигналов",
                "Извлечение руководителей и контактов",
                "Оценка релевантности",
            ],
            runner=_run_enrich,
        )
        return rec.as_dict()

    @app.post("/api/jobs/drafts")
    async def api_job_drafts(req: DraftsRequest):
        cfg = load_config()
        if not is_niche_configured(cfg):
            raise HTTPException(
                status_code=400,
                detail="Сначала заполните информацию о вашем проекте во вкладке «Проект»",
            )

        async def _run_dr(job: JobRecord) -> dict:
            job.set_step(0, percent=20, detail="Отбор релевантных получателей и сигналов")
            await score_all_prospects(state, use_ai=False, limit=200, config=cfg)
            min_s = cfg.icp.min_fit_score if req.min_score is None else req.min_score
            prospects = await state.get_prospects(min_score=min_s, limit=req.limit * 2)

            ready = 0
            blocked = 0
            skipped = 0
            target_list = prospects[: req.limit]
            total = max(1, len(target_list))

            job.set_step(1, percent=45, detail="Подготовка персональных писем")
            for idx, p in enumerate(target_list, start=1):
                pct = 40 + int((idx / total) * 52)
                job.set_step(
                    1,
                    percent=min(94, pct),
                    detail=f"Подготовка текста ({idx} из {len(target_list)}): {p.company or p.full_name}",
                )
                r = await generate_draft_for_prospect(
                    state,
                    p,
                    channel=req.channel,
                    mode_override="template",
                    config=cfg,
                )
                if r.get("gate_passed"):
                    ready += 1
                elif r.get("status") == "gate_blocked":
                    blocked += 1
                else:
                    skipped += 1
                await asyncio.sleep(0.05)

            job.set_step(2, percent=97, detail="Проверка качества и сохранение черновиков")
            await asyncio.sleep(0.1)
            res = {"drafts_ready": ready, "gate_blocked": blocked, "skipped": skipped}
            job.complete(
                res,
                summary_stage=f"Подготовлено черновиков: {ready}",
            )
            return res

        rec = jobs.start(
            kind="drafts",
            title="Подготовка черновиков",
            step_labels=[
                "Отбор получателей и сигналов",
                "Написание персональных обращений",
                "Финальная проверка текстов",
            ],
            runner=_run_dr,
        )
        return rec.as_dict()

    @app.post("/api/jobs/sample")
    async def api_job_sample():
        cfg = load_config()

        async def _run_smp(job: JobRecord) -> dict:
            job.set_step(0, percent=35, detail="Создание демонстрационных компаний под ваш профиль")
            await asyncio.sleep(0.15)
            stats = await seed_sample_for_active_niche(state, config=cfg)
            job.set_step(1, percent=85, detail="Расчёт релевантности и сохранение карточек")
            await asyncio.sleep(0.15)
            job.complete(
                stats,
                summary_stage=f"Добавлено демо-компаний: {stats.get('inserted_companies', 0)}",
            )
            return stats

        rec = jobs.start(
            kind="sample",
            title="Загрузка демо-данных",
            step_labels=[
                "Генерация карточек под выбранную нишу",
                "Оценка релевантности",
            ],
            runner=_run_smp,
        )
        return rec.as_dict()

    # ── Ниша и продукт (синхронные REST-маршруты и CLI-совместимость) ──

    @app.get("/api/niche/presets")
    async def api_niche_presets():
        return {"items": NICHE_TEMPLATES_RU}

    @app.post("/api/niche/save")
    async def api_niche_save(req: NicheSaveRequest):
        cfg = load_config()
        saved = apply_niche_setup(req.model_dump(), base_config=cfg)
        new_cfg = load_config()
        return {
            "ok": True,
            "saved": saved,
            "niche_configured": is_niche_configured(new_cfg),
            "config": new_cfg.model_dump(),
        }

    @app.post("/api/niche/train")
    async def api_niche_train(req: NicheTrainRequest):
        if not req.website.strip() and not req.brief.strip() and not req.niche_hint.strip():
            raise HTTPException(
                status_code=400,
                detail="Укажите сайт вашего продукта или краткое описание ниши и оффера",
            )
        cfg = load_config()
        crawl = await crawl_product_site(req.website) if req.website.strip() else {"url": "", "pages": [], "error": ""}
        profile_data = synthesize_niche_profile(
            website=req.website,
            crawl=crawl,
            brief=req.brief,
            niche_hint=req.niche_hint,
            cities=req.cities,
        )
        saved = apply_niche_setup(profile_data, base_config=cfg)
        new_cfg = load_config()
        return {
            "ok": True,
            "saved": saved,
            "niche_configured": is_niche_configured(new_cfg),
            "pages_crawled": len(crawl.get("pages") or []),
            "crawl_error": crawl.get("error", ""),
        }

    # ── Сигналы, Компании, ЛПР, Черновики ──

    @app.get("/api/signals")
    async def api_signals(status: str | None = None):
        return {"items": await state.list_signal_codes(status=status)}

    @app.patch("/api/signals/{code}")
    async def api_patch_signal(code: str, patch: StatusPatch):
        try:
            ok = await state.set_signal_status(code.upper(), patch.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="Код сигнала не найден")
        return {"ok": True, "code": code.upper(), "status": patch.status}

    @app.get("/api/companies")
    async def api_companies(require: str = "", exclude: str = "", limit: int = 200):
        req_codes = [c.strip().upper() for c in require.split(",") if c.strip()]
        exc_codes = [c.strip().upper() for c in exclude.split(",") if c.strip()]
        if req_codes or exc_codes:
            companies = await state.cohort(require=req_codes, exclude=exc_codes)
        else:
            companies = await state.get_companies(limit=limit)

        out = []
        for c in companies[:limit]:
            obs = await state.get_observations(company_id=c.id)
            d = asdict(c)
            d["observations"] = obs
            out.append(d)
        return {"items": out}

    @app.get("/api/prospects")
    async def api_prospects(min_score: int = 0, status: str | None = None, limit: int = 200):
        prospects = await state.get_prospects(status=status, min_score=min_score, limit=limit)
        out = []
        for p in prospects:
            d = asdict(p)
            d["full_name"] = p.full_name
            d["polite_greeting"] = p.polite_greeting
            out.append(d)
        return {"items": out}

    @app.get("/api/drafts")
    async def api_drafts(status: str | None = None, channel: str | None = None):
        return {"items": await state.list_drafts(status=status, channel=channel, limit=300)}

    @app.patch("/api/drafts/{draft_id}")
    async def api_patch_draft(draft_id: str, patch: StatusPatch):
        try:
            ok = await state.set_draft_status(draft_id, patch.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="Черновик не найден")
        return {"ok": True, "id": draft_id, "status": patch.status}

    # ── Прямые этапы конвейера ──

    @app.post("/api/run/discover")
    async def api_run_discover(req: DiscoverRequest):
        cfg = load_config()
        q = req.query.strip() or (cfg.icp.search_queries[0] if cfg.icp.search_queries else "")
        c = req.city.strip() or (cfg.icp.cities[0] if cfg.icp.cities else "Москва")
        if not q:
            raise HTTPException(
                status_code=400,
                detail="Укажите поисковый запрос или настройте профиль проекта",
            )
        return await run_discover(
            state,
            query=q,
            city=c,
            provider=req.provider,
            limit=req.limit,
            config=cfg,
        )

    @app.post("/api/import/upload")
    async def api_import_upload(
        file: UploadFile = File(...),
        city: str = Form(default=""),
        industry: str = Form(default=""),
    ):
        cfg = load_config()
        suffix = Path(file.filename or "upload.csv").suffix.lower()
        if suffix not in (".csv", ".json", ".jsonl", ".txt"):
            raise HTTPException(status_code=400, detail="Поддерживаются форматы .csv, .json, .jsonl, .txt")
        raw_bytes = await file.read()
        if len(raw_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Файл превышает лимит 10 МБ")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = Path(tmp.name)
        try:
            parsed = parse_import_file(
                tmp_path,
                default_city=city.strip() or (cfg.icp.cities[0] if cfg.icp.cities else ""),
                default_industry=industry.strip() or cfg.icp.niche,
            )
            stats = await persist_discovered_items(state, parsed, config=cfg, run_id="web_upload")
            await score_all_prospects(state, use_ai=False, limit=200, config=cfg)
            return {"ok": True, "filename": file.filename, **stats}
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.post("/api/run/profile")
    async def api_run_profile():
        return await run_profile(state, limit=30)

    @app.post("/api/run/prospects")
    async def api_run_prospects():
        cfg = load_config()
        return await run_people(state, limit=30, config=cfg)

    @app.post("/api/run/score")
    async def api_run_score(use_ai: bool = False):
        cfg = load_config()
        return await score_all_prospects(state, use_ai=use_ai, limit=200, config=cfg)

    @app.post("/api/run/drafts")
    async def api_run_drafts(req: DraftsRequest):
        cfg = load_config()
        return await run_drafts(
            state,
            channel=req.channel,
            min_score=req.min_score,
            limit=req.limit,
            mode_override="template" if req.template_preview else None,
            config=cfg,
        )

    @app.post("/api/run/draft-single")
    async def api_run_draft_single(req: SingleDraftRequest):
        p = await state.get_prospect(req.prospect_id)
        if not p:
            raise HTTPException(status_code=404, detail="Контакт не найден")
        cfg = load_config()
        return await generate_draft_for_prospect(
            state,
            p,
            channel=req.channel,
            mode_override="template" if req.template_preview else None,
            config=cfg,
        )

    @app.post("/api/run/sample")
    async def api_run_sample():
        cfg = load_config()
        return await seed_sample_for_active_niche(state, config=cfg)

    # ── Программные API-эндпоинты для CLI-агентов ──

    @app.get("/api/agent-tasks")
    async def api_agent_tasks(status: str = "pending"):
        raw_items = await state.list_agent_tasks(status=status)
        kind_labels = {
            "train_niche": "Настройка профиля проекта",
            "scout_score": "Квалификация и скоринг ЛПР",
            "writer_draft": "Подготовка персонального черновика",
        }
        enriched = []
        for item in raw_items:
            pl = item.get("payload") or {}
            kind = item.get("kind", "")
            target_desc = ""
            if kind == "train_niche":
                parts = [
                    f"Сайт: {pl['website']}" if pl.get("website") else "",
                    f"Ниша: {pl['niche_hint']}" if pl.get("niche_hint") else "",
                ]
                target_desc = " · ".join(x for x in parts if x) or "Настройка профиля"
            else:
                pid = str(pl.get("prospect_id") or "")
                p_obj = await state.get_prospect(pid) if pid else None
                c_obj = await state.get_company(str(pl.get("company_id") or "")) if pl.get("company_id") else None
                comp_name = c_obj.name if c_obj else (p_obj.company if p_obj else "")
                fio = p_obj.full_name if p_obj else pid
                target_desc = f"{comp_name} — {fio}".strip(" —")

            enriched.append(
                {
                    "id": item["id"],
                    "kind": kind,
                    "kind_label": kind_labels.get(kind, kind),
                    "status": item["status"],
                    "target": target_desc,
                    "payload": pl,
                    "created_at": item.get("created_at", ""),
                    "completed_at": item.get("completed_at") or "",
                }
            )
        return {"items": enriched}

    @app.post("/api/agent-tasks/{task_id}/complete")
    async def api_complete_agent_task(task_id: str, body: AgentTaskCompletion):
        cfg = load_config()
        try:
            outcome = await apply_agent_task_result(state, task_id, body.response_json, cfg=cfg)
            return {"ok": True, "outcome": outcome}
        except BrainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/agent-tasks/{task_id}")
    async def api_cancel_agent_task(task_id: str):
        ok = await state.cancel_agent_task(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Активная задача не найдена")
        return {"ok": True, "id": task_id, "status": "cancelled"}

    # ── Экспорт ──

    @app.get("/api/export/csv")
    async def api_export_csv(status: str | None = None):
        content = await export_drafts_csv(state, status=status)
        return Response(
            content=content.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="friday_drafts.csv"'},
        )

    @app.get("/api/export/markdown")
    async def api_export_md(status: str | None = None):
        content = await export_drafts_markdown(state, status=status)
        return PlainTextResponse(content)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index_page():
        return FileResponse(WEB_DIR / "index.html")

    return app
