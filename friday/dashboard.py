"""FastAPI веб-сервер F.R.I.D.A.Y. (0.0.0.0:5555, поддержка прокси Live Preview, относительные `/api/*`)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from html import escape
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from friday.agents.scout import score_all_prospects
from friday.agents.writer import generate_draft_for_prospect, run_drafts
from friday.brain import (
    BrainError,
    apply_agent_task_result,
)
from friday.collectors.discover import (
    DiscoveredItem,
    is_junk_ru_domain,
    parse_import_file,
    persist_discovered_items,
    run_discover,
)
from friday.collectors.people import extract_people_from_html, run_people
from friday.collectors.profile import inspect_company_html, run_profile
from friday.config import is_niche_configured, load_config
from friday.export import export_drafts_csv, export_drafts_markdown
from friday.gate import validate_draft
from friday.jobs import JobManager, JobRecord
from friday.models.prospect import COMPANY_CONTACT_SOURCE, Prospect
from friday.paths import SKILLS_DIR
from friday.pipeline import bootstrap_state, seed_sample_for_active_niche
from friday.signals import category_label_ru, signal_label_ru
from friday.state import StateManager
from friday.trainer import (
    NICHE_TEMPLATES_RU,
    apply_niche_setup,
    crawl_product_site,
    synthesize_niche_profile,
)

WEB_DIR = Path(__file__).resolve().parent / "web"

_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:https?://|mailto:|tel:|/)[^)\s]+)\)")


def _markdown_page_to_html(content: str, title: str, page_url: str) -> str:
    """Преобразует живой слепок страницы (HTML или Jina Reader Markdown) в HTML для детерминированного профайлера."""
    if "<html" in content.lower() or "<body" in content.lower():
        return content

    parsed_base = urlparse(page_url or "")
    base_host = (parsed_base.hostname or "").lower().removeprefix("www.")

    def _repl_img(m: re.Match) -> str:
        alt = escape(m.group(1) or "")
        src = m.group(2)
        return f'<img src="{escape(src)}" alt="{alt}">'

    def _repl_link(m: re.Match) -> str:
        label = escape(m.group(1) or "")
        href = m.group(2)
        p = urlparse(href)
        h_host = (p.hostname or "").lower().removeprefix("www.")
        if base_host and h_host == base_host and p.path and p.path != "/":
            rel = p.path
            return f'<a href="{escape(rel)}">{label}</a> <a href="{escape(href)}">{label}</a>'
        return f'<a href="{escape(href)}">{label}</a>'

    body_html = _MD_IMAGE_RE.sub(_repl_img, content)
    body_html = _MD_LINK_RE.sub(_repl_link, body_html)

    form_tag = ""
    low = content.lower()
    if any(
        kw in low
        for kw in (
            "оставить заявку",
            "заказать звонок",
            "записаться на консультацию",
            "обратный звонок",
            "отправить заявку",
            "получить консультацию",
            "онлайн-запись",
        )
    ):
        form_tag = '<form action="/lead"><input type="tel"></form>'

    safe_title = escape(title.strip()) if title else ""
    return (
        f"<!doctype html><html><head>"
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{safe_title}</title></head><body>"
        f"{form_tag}\n{body_html}"
        f"</body></html>"
    )


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
    crawled_title: str = ""
    crawled_text: str = ""


class DiscoverRequest(BaseModel):
    query: str = ""
    city: str = ""
    provider: str = "auto"
    limit: int = Field(default=25, ge=1, le=200)


class DraftsRequest(BaseModel):
    channel: str = "telegram"
    min_score: int | None = None
    city: str = ""
    limit: int = Field(default=50, ge=1, le=200)
    template_preview: bool = False


class SingleDraftRequest(BaseModel):
    prospect_id: str
    channel: str = "telegram"
    template_preview: bool = False


class PrepareProposalRequest(BaseModel):
    company_id: str = ""
    prospect_id: str = ""
    channel: str = "telegram"


class DraftContentPatch(BaseModel):
    subject: str = ""
    body: str


class StatusPatch(BaseModel):
    status: str


class AgentTaskCompletion(BaseModel):
    response_json: str


class WorkspaceClearRequest(BaseModel):
    city: str = ""


class LiveBatchItem(BaseModel):
    name: str
    website: str = ""
    city: str = ""
    industry: str = ""
    company_size: str = ""
    inn: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    contact_fio: str = ""
    contact_title: str = ""
    telegram: str = ""
    whatsapp: str = ""
    vk: str = ""
    ymaps_url: str = ""
    twogis_url: str = ""
    hh_url: str = ""
    source_url: str = ""
    external_id: str = ""
    source: str = "web_ru"
    description: str = ""
    observations: list[dict] = Field(default_factory=list)


class LiveBatchRequest(BaseModel):
    items: list[LiveBatchItem] = Field(default_factory=list)


class LivePageItem(BaseModel):
    company_id: str
    url: str
    title: str = ""
    content: str = ""


class LivePagesEnrichRequest(BaseModel):
    pages: list[LivePageItem] = Field(default_factory=list)


def create_app(db_path: str | None = None) -> FastAPI:
    state = StateManager(db_path=db_path)
    jobs = JobManager()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        await bootstrap_state(state, auto_confirm_core=True)
        yield

    app = FastAPI(
        title="F.R.I.D.A.Y.",
        version="0.4.0",
        lifespan=_lifespan,
    )

    @app.middleware("http")
    async def _preview_headers(request: Request, call_next):
        resp = await call_next(request)
        if "X-Frame-Options" in resp.headers:
            del resp.headers["X-Frame-Options"]
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp

    @app.get("/api/health")
    async def api_health():
        cfg = load_config()
        return {"ok": True, "version": "0.4.0", "niche_configured": is_niche_configured(cfg)}

    @app.get("/api/summary")
    async def api_summary():
        cfg = load_config()
        summary = await state.get_state_summary()
        cities = await state.list_distinct_cities()
        prod_skill = SKILLS_DIR / "product_knowledge.md"
        comp_skill = SKILLS_DIR / "competitive_intel.md"
        return {
            "summary": summary,
            "cities": cities,
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
            await asyncio.sleep(0.1)
            crawl = (
                await crawl_product_site(req.website, timeout=4.5)
                if req.website.strip()
                else {"url": "", "pages": [], "error": ""}
            )
            if not (crawl.get("pages") or []) and req.crawled_text.strip():
                crawl = {
                    "url": req.website.strip(),
                    "pages": [
                        {
                            "url": req.website.strip(),
                            "title": req.crawled_title.strip(),
                            "h1": req.crawled_title.strip(),
                            "description": "",
                            "snippet": req.crawled_text.strip()[:3500],
                        }
                    ],
                    "error": "",
                }

            job.set_step(1, percent=52, detail="Формирование портрета клиентов и поисковых запросов")
            await asyncio.sleep(0.15)
            profile_data = synthesize_niche_profile(
                website=req.website,
                crawl=crawl,
                brief=req.brief,
                niche_hint=req.niche_hint,
                cities=req.cities,
            )

            job.set_step(2, percent=82, detail="Настройка критериев отбора и параметров тона")
            await asyncio.sleep(0.15)
            cfg = load_config()
            saved = apply_niche_setup(profile_data, base_config=cfg)

            job.set_step(3, percent=96, detail="Сохранение профиля проекта")
            await asyncio.sleep(0.08)
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
            if disc.get("network_isolated"):
                res = {
                    "network_isolated": True,
                    "query": q,
                    "city": c,
                    "provider": req.provider,
                    "limit": req.limit,
                    "search_queries": cfg.icp.search_queries[:5],
                    "inserted_companies": 0,
                    "prospects_saved": 0,
                    "scored": 0,
                }
                job.complete(res, summary_stage="Переключение на прямое получение данных")
                return res

            job.set_step(1, percent=65, detail="Проверка сайтов и извлечение контактов ЛПР")
            prof = await run_profile(state, limit=max(10, req.limit))
            peop = await run_people(state, limit=max(10, req.limit), config=cfg)

            job.set_step(2, percent=90, detail="Оценка релевантности найденных карточек")
            scor = await score_all_prospects(state, use_ai=False, limit=300, config=cfg)
            found_companies = disc.get("inserted_companies", 0) + disc.get("updated_companies", 0)
            found_prospects = disc.get("inserted_prospects", 0) + peop.get("prospects_saved", 0)
            res = {
                "network_isolated": False,
                "city": c,
                "inserted_companies": disc.get("inserted_companies", 0),
                "updated_companies": disc.get("updated_companies", 0),
                "profiled": prof.get("checked", 0),
                "prospects_saved": found_prospects,
                "scored": scor.get("scored", 0),
                "providers_used": disc.get("providers_used", []),
            }
            job.complete(
                res,
                summary_stage=f"Найдено компаний ({c}): {found_companies} · Контактов ЛПР: {found_prospects}",
            )
            return res

        rec = jobs.start(
            kind="discover",
            title=f"Поиск компаний ({c})",
            step_labels=[
                f"Поиск по запросу «{q}» в г. {c}",
                "Анализ сайтов и контактов ЛПР",
                "Оценка релевантности",
            ],
            runner=_run_disc,
        )
        return rec.as_dict()

    @app.post("/api/jobs/enrich")
    async def api_job_enrich(city: str = ""):
        cfg = load_config()

        async def _run_enrich(job: JobRecord) -> dict:
            job.set_step(0, percent=25, detail="Анализ сайтов и маркетинговых сигналов")
            prof = await run_profile(state, limit=50)
            job.set_step(1, percent=65, detail="Поиск руководителей и прямых контактов")
            peop = await run_people(state, limit=50, config=cfg)
            job.set_step(2, percent=90, detail="Расчёт оценки соответствия профилю")
            scor = await score_all_prospects(state, use_ai=False, limit=300, config=cfg)

            candidates = await state.get_companies(limit=80, city=city or None)
            web_targets = [
                {"company_id": c.id, "name": c.name, "website": c.website or (f"https://{c.domain}" if c.domain else "")}
                for c in candidates
                if (c.website or c.domain)
            ]
            isolated = prof.get("checked", 0) == 0 and len(web_targets) > 0 and prof.get("unreachable", 0) >= 0

            res = {
                "network_isolated": isolated and prof.get("checked", 0) == 0,
                "targets": web_targets[:40] if prof.get("checked", 0) == 0 else [],
                "profiled": prof.get("checked", 0),
                "prospects_saved": peop.get("prospects_saved", 0),
                "scored": scor.get("scored", 0),
            }
            job.complete(
                res,
                summary_stage=(
                    "Подготовка прямого аудита сайтов"
                    if res["network_isolated"]
                    else f"Проверено сайтов: {res['profiled']} · Контактов ЛПР: {res['scored']}"
                ),
            )
            return res

        rec = jobs.start(
            kind="enrich",
            title="Аудит сайтов и поиск ЛПР",
            step_labels=[
                "Анализ сайтов и сигналов",
                "Извлечение руководителей и контактов",
                "Оценка релевантности",
            ],
            runner=_run_enrich,
        )
        return rec.as_dict()

    @app.post("/api/enrich/live-pages")
    async def api_enrich_live_pages(req: LivePagesEnrichRequest):
        """Детерминированный разбор живых страниц компаний (аудит стека + сигналы + извлечение ФИО ЛПР)."""
        cfg = load_config()
        profiled = 0
        people_added = 0
        obs_added = 0

        for page in req.pages[:50]:
            comp = await state.get_company(page.company_id)
            if not comp or not page.content.strip():
                continue

            html_doc = _markdown_page_to_html(page.content, page.title, page.url or comp.website)
            parsed = inspect_company_html(
                html_doc,
                final_url=page.url or comp.website or f"https://{comp.domain}",
                elapsed_ms=650.0,
                had_https_redirect=(page.url or comp.website).startswith("https://"),
            )

            await state.update_company_profile_fields(
                comp.id,
                name=parsed.get("title", "")[:120] if not comp.name else "",
                inn=parsed.get("inn", ""),
                description=parsed.get("description", ""),
                phone=parsed.get("phone", ""),
                telegram=parsed.get("telegram", ""),
                whatsapp=parsed.get("whatsapp", ""),
                vk=parsed.get("vk", ""),
                tenchat=parsed.get("tenchat", ""),
                tech_stack=parsed.get("tech_stack") or [],
                signals=[o["signal_code"] for o in parsed.get("observations") or []],
            )
            for obs in parsed.get("observations") or []:
                oid = await state.record_observation(
                    signal_code=obs["signal_code"],
                    company_id=comp.id,
                    collector="profile",
                    value_num=obs.get("value_num"),
                    value_text=obs.get("value_text", ""),
                    evidence_url=obs.get("evidence_url") or page.url,
                    require_confirmed=True,
                )
                if oid:
                    obs_added += 1

            refreshed_comp = await state.get_company(comp.id) or comp
            found_people = extract_people_from_html(
                html_doc,
                page_url=page.url or comp.website,
                company=refreshed_comp,
                custom_titles=cfg.icp.decision_maker_titles,
            )
            for person in found_people:
                pid, created = await state.upsert_prospect(person)
                if created:
                    people_added += 1
                    await state.record_observation(
                        signal_code="DECISION_MAKER_FOUND",
                        company_id=comp.id,
                        prospect_id=pid,
                        collector="people",
                        value_text=f"{person.full_name} — {person.title}",
                        evidence_url=page.url or comp.website,
                        require_confirmed=True,
                    )
                if person.email and person.email_source == "personal_page":
                    await state.record_observation(
                        signal_code="PERSONAL_EMAIL_FOUND",
                        company_id=comp.id,
                        prospect_id=pid,
                        collector="people",
                        value_text=person.email,
                        evidence_url=page.url or comp.website,
                        require_confirmed=True,
                    )

            if not found_people:
                await state.ensure_company_prospect(comp.id)

            await state.mark_prospects_checked(comp.id)
            profiled += 1

        scor = await score_all_prospects(state, use_ai=False, limit=300, config=cfg)
        return {
            "ok": True,
            "profiled": profiled,
            "observations_recorded": obs_added,
            "prospects_added": people_added,
            "scored": scor.get("scored", 0),
        }

    @app.post("/api/jobs/drafts")
    async def api_job_drafts(req: DraftsRequest):
        cfg = load_config()
        if not is_niche_configured(cfg):
            raise HTTPException(
                status_code=400,
                detail="Сначала настройте параметры вашего проекта во вкладке «Проект»",
            )

        async def _run_dr(job: JobRecord) -> dict:
            city_scope = req.city.strip()
            job.set_step(0, percent=18, detail="Отбор организаций, контактов и подтверждённых сигналов")
            companies = await state.get_companies(limit=max(req.limit * 2, 100), city=city_scope or None)
            for comp in companies[: req.limit]:
                await state.ensure_company_prospect(comp.id)

            await score_all_prospects(state, use_ai=False, limit=300, config=cfg)
            min_s = cfg.icp.min_fit_score if req.min_score is None else req.min_score
            prospects = await state.get_prospects(
                min_score=min_s,
                city=city_scope or None,
                limit=req.limit * 2,
            )
            if not prospects and min_s > 0:
                prospects = await state.get_prospects(
                    min_score=0,
                    city=city_scope or None,
                    limit=req.limit * 2,
                )

            ready = 0
            blocked = 0
            skipped = 0
            target_list = prospects[: req.limit]
            total = max(1, len(target_list))

            job.set_step(1, percent=42, detail="Подготовка персональных предложений")
            for idx, p in enumerate(target_list, start=1):
                pct = 40 + int((idx / total) * 52)
                job.set_step(
                    1,
                    percent=min(94, pct),
                    detail=f"Подготовка предложения ({idx} из {len(target_list)}): {p.company or p.full_name}",
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
                await asyncio.sleep(0.03)

            job.set_step(2, percent=97, detail="Проверка качества и сохранение предложений")
            await asyncio.sleep(0.06)
            res = {
                "drafts_ready": ready,
                "gate_blocked": blocked,
                "skipped": skipped,
                "city": city_scope,
            }
            job.complete(
                res,
                summary_stage=f"Подготовлено предложений: {ready}",
            )
            return res

        scope_label = f" ({req.city.strip()})" if req.city.strip() else ""
        rec = jobs.start(
            kind="drafts",
            title=f"Подготовка предложений{scope_label}",
            step_labels=[
                "Отбор организаций и сигналов аудита",
                "Формирование персональных предложений",
                "Проверка соответствия правилам (Gate)",
            ],
            runner=_run_dr,
        )
        return rec.as_dict()

    @app.post("/api/proposals/prepare")
    async def api_prepare_proposal(req: PrepareProposalRequest):
        """Мгновенная подготовка персонального предложения по конкретной компании или ЛПР."""
        cfg = load_config()
        if not is_niche_configured(cfg):
            raise HTTPException(
                status_code=400,
                detail="Сначала настройте параметры вашего проекта во вкладке «Проект»",
            )
        prospect: Prospect | None = None
        if req.prospect_id.strip():
            prospect = await state.get_prospect(req.prospect_id.strip())
        elif req.company_id.strip():
            prospect = await state.ensure_company_prospect(req.company_id.strip())

        if not prospect:
            raise HTTPException(status_code=404, detail="Организация или контакт не найдены")

        await score_all_prospects(state, use_ai=False, limit=250, config=cfg)
        prospect = await state.get_prospect(prospect.id) or prospect

        res = await generate_draft_for_prospect(
            state,
            prospect,
            channel=req.channel,
            mode_override="template",
            config=cfg,
        )
        draft_obj = await state.get_draft(res["draft_id"]) if res.get("draft_id") else None
        return {"ok": True, "result": res, "draft": draft_obj}

    # ── Ниша и продукт ──

    @app.get("/api/niche/presets")
    async def api_niche_presets(request: Request, response: Response, ui: str = ""):
        # Если браузер выполняет старый закэшированный JS (без ?ui=5), принудительно сбрасываем HTTP-кэш домена предпросмотра
        if ui != "5" and "testclient" not in (request.headers.get("user-agent") or "").lower():
            response.headers["Clear-Site-Data"] = '"cache"'
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
        if not (crawl.get("pages") or []) and req.crawled_text.strip():
            crawl = {
                "url": req.website.strip(),
                "pages": [
                    {
                        "url": req.website.strip(),
                        "title": req.crawled_title.strip(),
                        "h1": req.crawled_title.strip(),
                        "description": "",
                        "snippet": req.crawled_text.strip()[:3500],
                    }
                ],
                "error": "",
            }
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

    # ── Сигналы, Компании, ЛПР, Предложения (Черновики) ──

    @app.get("/api/signals")
    async def api_signals(status: str | None = None):
        raw = await state.list_signal_codes(status=status)
        items = [
            {
                **row,
                "category_label": category_label_ru(row.get("category", "")),
            }
            for row in raw
        ]
        return {"items": items}

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
    async def api_companies(
        require: str = "",
        exclude: str = "",
        city: str = "",
        limit: int = 200,
    ):
        req_codes = [c.strip().upper() for c in require.split(",") if c.strip()]
        exc_codes = [c.strip().upper() for c in exclude.split(",") if c.strip()]
        if req_codes or exc_codes:
            companies = await state.cohort(require=req_codes, exclude=exc_codes, city=city or None)
        else:
            companies = await state.get_companies(limit=limit, city=city or None)

        out = []
        for c in companies[:limit]:
            obs = await state.get_observations(company_id=c.id)
            enriched_obs = [
                {
                    **o,
                    "signal_label": signal_label_ru(o.get("signal_code", "")),
                }
                for o in obs
            ]
            d = asdict(c)
            d["observations"] = enriched_obs
            d["signals_ru"] = [signal_label_ru(sc) for sc in (c.signals or [])]
            out.append(d)
        return {"items": out}

    @app.delete("/api/companies/{company_id}")
    async def api_delete_company(company_id: str):
        ok = await state.delete_company(company_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Компания не найдена")
        return {"ok": True, "id": company_id}

    @app.post("/api/workspace/clear")
    async def api_workspace_clear(req: WorkspaceClearRequest):
        res = await state.clear_workspace_data(city=req.city or None)
        return {"ok": True, **res}

    @app.get("/api/prospects")
    async def api_prospects(
        min_score: int = 0,
        status: str | None = None,
        city: str = "",
        company_id: str = "",
        limit: int = 200,
    ):
        prospects = await state.get_prospects(
            status=status,
            min_score=min_score,
            company_id=company_id or None,
            city=city or None,
            limit=limit,
        )
        out = []
        for p in prospects:
            d = asdict(p)
            d["full_name"] = p.full_name
            out.append(d)
        return {"items": out}

    @app.get("/api/drafts")
    async def api_drafts(
        status: str | None = None,
        channel: str | None = None,
        city: str = "",
    ):
        return {
            "items": await state.list_drafts(
                status=status,
                channel=channel,
                city=city or None,
                limit=300,
            )
        }

    @app.patch("/api/drafts/{draft_id}")
    async def api_patch_draft(draft_id: str, patch: StatusPatch):
        try:
            ok = await state.set_draft_status(draft_id, patch.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="Предложение не найдено")
        return {"ok": True, "id": draft_id, "status": patch.status}

    @app.patch("/api/drafts/{draft_id}/content")
    async def api_patch_draft_content(draft_id: str, patch: DraftContentPatch):
        existing = await state.get_draft(draft_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Предложение не найдено")
        cfg = load_config()
        gate_res = validate_draft(
            channel=existing["channel"],
            subject=patch.subject,
            body=patch.body,
            company_domain=existing.get("domain") or "",
            prospect_email=existing.get("email") or "",
            prospect_telegram=existing.get("prospect_telegram") or existing.get("company_telegram") or "",
            config=cfg,
        )
        new_status = "pending_review" if gate_res.passed else "gate_blocked"
        notes = [*gate_res.reasons, *gate_res.warnings]
        await state.update_draft_content(
            draft_id,
            subject=patch.subject,
            body=patch.body,
            status=new_status,
            gate_notes=notes,
        )
        updated = await state.get_draft(draft_id)
        return {
            "ok": True,
            "gate_passed": gate_res.passed,
            "status": new_status,
            "gate_notes": notes,
            "draft": updated,
        }

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
            await score_all_prospects(state, use_ai=False, limit=300, config=cfg)
            return {"ok": True, "filename": file.filename, **stats}
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.post("/api/import/live-batch")
    async def api_import_live_batch(req: LiveBatchRequest):
        cfg = load_config()
        default_role = (
            cfg.icp.decision_maker_titles[0].capitalize()
            if cfg.icp.decision_maker_titles
            else "Руководитель"
        )
        pairs: list[tuple[DiscoveredItem, Prospect | None]] = []

        for raw in req.items[:200]:
            name = raw.name.strip()
            if not name:
                continue
            website = raw.website.strip()
            if website and not website.startswith(("http://", "https://")):
                website = f"https://{website}"
            if website and is_junk_ru_domain(website):
                website = ""

            hh_url = raw.hh_url.strip()
            ymaps_url = raw.ymaps_url.strip()
            twogis_url = raw.twogis_url.strip()
            source_url = raw.source_url.strip() or website or ymaps_url or twogis_url or hh_url
            if not website and not hh_url and not ymaps_url and not twogis_url and not raw.phone.strip() and not raw.email.strip():
                continue

            obs_clean: list[dict] = []
            for o in raw.observations:
                code = str(o.get("signal_code") or "").strip().upper()
                if not code:
                    continue
                obs_clean.append(
                    {
                        "signal_code": code,
                        "collector": str(o.get("collector") or raw.source or "live"),
                        "value_num": o.get("value_num"),
                        "value_text": str(o.get("value_text") or "")[:240],
                        "evidence_url": str(o.get("evidence_url") or source_url)[:300],
                    }
                )

            item = DiscoveredItem(
                name=name,
                website=website,
                city=raw.city.strip(),
                industry=raw.industry.strip() or cfg.icp.niche,
                company_size=raw.company_size.strip(),
                inn=raw.inn.strip(),
                address=raw.address.strip(),
                phone=raw.phone.strip(),
                telegram=raw.telegram.strip(),
                whatsapp=raw.whatsapp.strip(),
                vk=raw.vk.strip(),
                ymaps_url=ymaps_url,
                twogis_url=twogis_url,
                hh_url=hh_url,
                external_id=raw.external_id.strip(),
                source=raw.source.strip() or "web_ru",
                source_url=source_url,
                description=raw.description.strip()[:400],
                observations=obs_clean,
            )

            parts = [p for p in raw.contact_fio.strip().split() if p]
            last_n = parts[0] if len(parts) >= 2 else ""
            first_n = parts[1] if len(parts) >= 2 else (parts[0] if len(parts) == 1 else "")
            mid_n = parts[2] if len(parts) >= 3 else ""

            prospect = Prospect(
                last_name=last_n,
                first_name=first_n,
                middle_name=mid_n,
                title=raw.contact_title.strip() or default_role,
                role_kind="icp_decision_maker" if first_n else "company_contact",
                email=raw.email.strip(),
                email_source="public_contact" if raw.email.strip() else "",
                phone=raw.phone.strip(),
                telegram=raw.telegram.strip(),
                whatsapp=raw.whatsapp.strip(),
                vk=raw.vk.strip(),
                company=name,
                industry=item.industry,
                city=item.city,
                source=COMPANY_CONTACT_SOURCE if not first_n else "public_contact",
                source_url=source_url,
            )
            pairs.append((item, prospect))

        stats = await persist_discovered_items(state, pairs, config=cfg, run_id="live_browser_batch")
        scor = await score_all_prospects(state, use_ai=False, limit=400, config=cfg)
        return {"ok": True, **stats, "scored": scor.get("scored", 0)}

    @app.post("/api/run/profile")
    async def api_run_profile():
        return await run_profile(state, limit=50)

    @app.post("/api/run/prospects")
    async def api_run_prospects():
        cfg = load_config()
        return await run_people(state, limit=50, config=cfg)

    @app.post("/api/run/score")
    async def api_run_score(use_ai: bool = False):
        cfg = load_config()
        return await score_all_prospects(state, use_ai=use_ai, limit=300, config=cfg)

    @app.post("/api/run/drafts")
    async def api_run_drafts(req: DraftsRequest):
        cfg = load_config()
        return await run_drafts(
            state,
            channel=req.channel,
            min_score=req.min_score,
            limit=req.limit,
            city=req.city or None,
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
            "writer_draft": "Подготовка персонального предложения",
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
            headers={"Content-Disposition": 'attachment; filename="friday_proposals.csv"'},
        )

    @app.get("/api/export/markdown")
    async def api_export_md(status: str | None = None):
        content = await export_drafts_markdown(state, status=status)
        return PlainTextResponse(content)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.api_route("/", methods=["GET", "HEAD"])
    async def index_page():
        return FileResponse(
            WEB_DIR / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    return app
