"""FastAPI веб-сервер F.R.I.D.A.Y. (0.0.0.0:5555, любые хосты/прокси Live Preview, относительные `/api/*`).

Ключевые возможности:
- Шаг 1 («1. Ниша и продукт»): настройка любой ниши рынка РФ (`friday.local.yaml` + `skills/product_knowledge.md`),
  краулинг сайта продукта и постановка задачи `train_niche` ИИ-агенту рабочей среды.
- Поиск и импорт компаний (включая открытый API `hh.ru`, Яндекс, 2ГИС, загрузку CSV/JSON/TXT).
- Центр задач Мозга (Вариант А — `agent_tasks`): просмотр пакетов задач и применение JSON-ответов агента.
- Проверка черновиков, шлюз ФЗ-38 (`gate.py`) и экспорт CSV / Markdown (без автоотправки).
"""

from __future__ import annotations

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
from friday.paths import SKILLS_DIR
from friday.pipeline import bootstrap_state, seed_sample_for_active_niche
from friday.state import StateManager
from friday.trainer import (
    NICHE_TEMPLATES_RU,
    apply_niche_setup,
    build_train_prompt_context,
    crawl_product_site,
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

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # Инициализируем только схему SQLite (WAL) и каталог из 26 универсальных сигналов РФ.
        # Никакая конкретная ниша не предзагружается: Шаг 1 задаёт сам пользователь!
        await bootstrap_state(state, auto_confirm_core=True)
        yield

    app = FastAPI(
        title="F.R.I.D.A.Y. — B2B-разведка и подготовка черновиков (РФ)",
        version="0.2.0",
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
        return {"ok": True, "version": "0.2.0", "niche_configured": is_niche_configured(cfg)}

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
            "skills": {
                "product_knowledge_md": prod_skill.read_text(encoding="utf-8") if prod_skill.exists() else "",
                "competitive_intel_md": comp_skill.read_text(encoding="utf-8") if comp_skill.exists() else "",
            },
        }

    # ── Шаг 1: Ниша и продукт ──

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
        sys_prompt = "\n\n".join(
            p for p in [load_prompt("system"), load_prompt("train"), load_skills(["ru_compliance"])] if p
        )
        user_prompt = build_train_prompt_context(
            website_crawl=crawl,
            user_brief=req.brief,
            target_niche_hint=req.niche_hint,
            cities_hint=req.cities or None,
        )
        tid, tfile = await enqueue_agent_task(
            state,
            cfg,
            kind="train_niche",
            payload={
                "website": req.website.strip(),
                "brief": req.brief.strip(),
                "niche_hint": req.niche_hint.strip(),
                "cities": req.cities,
                "pages_crawled": len(crawl.get("pages") or []),
            },
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
        )
        return {
            "ok": True,
            "task_id": tid,
            "task_file": tfile,
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

    # ── Этапы конвейера ──

    @app.post("/api/run/discover")
    async def api_run_discover(req: DiscoverRequest):
        cfg = load_config()
        q = req.query.strip() or (cfg.icp.search_queries[0] if cfg.icp.search_queries else "")
        c = req.city.strip() or (cfg.icp.cities[0] if cfg.icp.cities else "Москва")
        if not q:
            raise HTTPException(
                status_code=400,
                detail="Укажите поисковый запрос или настройте нишу на вкладке «1. Ниша и продукт»",
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
            raise HTTPException(status_code=404, detail="ЛПР не найден")
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

    # ── Очередь задач Мозга (Вариант А — ИИ-агент рабочей среды) ──

    @app.get("/api/agent-tasks")
    async def api_agent_tasks(status: str = "pending"):
        return {"items": await state.list_agent_tasks(status=status)}

    @app.post("/api/agent-tasks/{task_id}/complete")
    async def api_complete_agent_task(task_id: str, body: AgentTaskCompletion):
        cfg = load_config()
        try:
            outcome = await apply_agent_task_result(state, task_id, body.response_json, cfg=cfg)
            return {"ok": True, "outcome": outcome}
        except BrainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Экспорт ──

    @app.get("/api/export/csv")
    async def api_export_csv(status: str | None = None):
        content = await export_drafts_csv(state, status=status)
        return Response(
            content=content.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="friday_drafts_ru.csv"'},
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
