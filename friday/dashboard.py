"""Веб-панель управления F.R.I.D.A.Y. (FastAPI + чистый HTML/CSS/JS без шага сборки).

Работает одинаково надёжно локально (`127.0.0.1:5555`) и в облачном Live Preview (`0.0.0.0`),
используя только относительные маршруты `/api/...`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

from friday.agents.scout import Scout
from friday.agents.writer import Writer
from friday.brain import Brain
from friday.collectors.discover import PROVIDERS, parse_import_text, persist_discovered_practices
from friday.config import FridayConfig, load_config, load_env
from friday.export import export_dossiers_markdown, export_prospects_csv
from friday.pipeline import apply_agent_task_result, run_profile_stage
from friday.register import build_company_register, register_to_csv
from friday.signals import FREE_SIGNAL_CODES, seed_signal_catalog
from friday.state import StateManager

logger = logging.getLogger("friday.dashboard")

WEB_DIR = Path(__file__).resolve().parent / "web"


class SignalStatusUpdate(BaseModel):
    status: str


class BulkSignalAction(BaseModel):
    mode: str   # confirm_free | confirm_all | reset_proposed


class DraftStatusUpdate(BaseModel):
    status: str
    body: str | None = None
    subject: str | None = None


class ImportPayload(BaseModel):
    content: str
    is_registry: bool = False
    default_city: str = ""


class PipelineActionPayload(BaseModel):
    min_score: int = 40
    limit: int = 15
    provider_override: str = ""


class AgentTaskSubmit(BaseModel):
    response_json: str


def create_app(db_path: str | Path | None = None, config_path: str | Path | None = None) -> FastAPI:
    def _state() -> StateManager:
        return StateManager(db_path=db_path)

    def _cfg() -> FridayConfig:
        try:
            return load_config(config_path)
        except Exception:
            return FridayConfig()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        st = _state()
        await st.init_db()
        await seed_signal_catalog(st, auto_confirm_free=False)
        yield

    app = FastAPI(title="F.R.I.D.A.Y. Dashboard", lifespan=_lifespan)

    @app.get("/api/overview")
    async def api_overview():
        st = _state()
        await st.init_db()
        await seed_signal_catalog(st, auto_confirm_free=False)
        cfg = _cfg()
        env = load_env()
        brain = Brain(st, brain_config=cfg.brain, env=env)
        ok, msg = await brain.preflight()
        summary = await st.get_state_summary()
        return {
            "summary": summary,
            "persona": cfg.persona.model_dump(),
            "bureau": cfg.bureau.model_dump(),
            "icp": cfg.icp.model_dump(),
            "compliance": cfg.compliance.model_dump(),
            "brain": {
                "provider": cfg.brain.provider,
                "model": cfg.brain.model,
                "ready": ok,
                "message": msg,
            },
            "providers": [
                {
                    "key": p.key,
                    "label": p.label,
                    "kind": p.kind,
                    "blurb": p.blurb,
                    "cost_per_query_rub": p.cost_per_query_rub,
                    "configured": p.configured(env),
                    "missing_keys": p.missing_keys(env),
                }
                for p in PROVIDERS.values()
            ],
        }

    @app.get("/api/signals")
    async def api_signals():
        st = _state()
        await st.init_db()
        await seed_signal_catalog(st, auto_confirm_free=False)
        codes = await st.list_signal_codes()
        for c in codes:
            c["is_free"] = c["code"] in FREE_SIGNAL_CODES
        return {"signals": codes}

    @app.post("/api/signals/bulk")
    async def api_signals_bulk(req: BulkSignalAction):
        st = _state()
        await st.init_db()
        all_codes = await st.list_signal_codes()
        updated = 0
        for row in all_codes:
            code = row["code"]
            if req.mode == "confirm_free" and code in FREE_SIGNAL_CODES:
                await st.set_signal_status(code, "confirmed")
                updated += 1
            elif req.mode == "confirm_all":
                await st.set_signal_status(code, "confirmed")
                updated += 1
            elif req.mode == "reset_proposed":
                await st.set_signal_status(code, "proposed")
                updated += 1
        return {"updated": updated}

    @app.post("/api/signals/{code}")
    async def api_signal_set(code: str, req: SignalStatusUpdate):
        st = _state()
        await st.init_db()
        try:
            changed = await st.set_signal_status(code, req.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not changed:
            raise HTTPException(status_code=404, detail="Сигнал не найден")
        return {"ok": True, "code": code, "status": req.status}

    @app.get("/api/companies")
    async def api_companies():
        st = _state()
        await st.init_db()
        cfg = _cfg()
        rows = await build_company_register(st, cfg)
        return {"companies": rows}

    @app.get("/api/drafts")
    async def api_drafts(status: str | None = None, channel: str | None = None):
        st = _state()
        await st.init_db()
        items = await st.list_drafts(status=status, channel=channel, limit=300)
        return {"drafts": items}

    @app.post("/api/drafts/{draft_id}")
    async def api_update_draft(draft_id: str, req: DraftStatusUpdate):
        st = _state()
        await st.init_db()
        cfg = _cfg()
        existing = [d for d in await st.list_drafts(limit=500) if d["id"] == draft_id]
        if not existing:
            raise HTTPException(status_code=404, detail="Черновик не найден")
        cur = existing[0]

        # Если текст отредактировали вручную — прогоняем через комплаенс-фильтр Gate
        if req.body is not None or req.subject is not None:
            from friday.gate import check_draft
            from friday.models import Draft

            new_body = req.body if req.body is not None else cur["body"]
            new_subj = req.subject if req.subject is not None else cur["subject"]
            gate = check_draft(
                channel=cur["channel"],
                subject=new_subj,
                body=new_body,
                config=cfg,
                domain=cur.get("domain") or "",
                email=cur.get("email") or "",
                telegram=cur.get("prospect_telegram") or cur.get("company_telegram") or "",
            )
            if not gate.ok and req.status in ("approved", "sent_manually"):
                raise HTTPException(
                    status_code=400,
                    detail="Фильтр ФЗ-38 / КПЭА отклонил правку: " + "; ".join(gate.reasons),
                )
            await st.upsert_draft(
                Draft(
                    id=draft_id,
                    prospect_id=cur["prospect_id"],
                    company_id=cur["company_id"],
                    channel=cur["channel"],
                    subject=new_subj,
                    body=new_body,
                    audit_summary=cur.get("audit_summary") or "",
                    signal_used=cur.get("signal_used") or "",
                    status=req.status if gate.ok or req.status == "rejected" else "gate_blocked",
                    gate_notes=[*gate.reasons, *(f"предупреждение: {w}" for w in gate.warnings)],
                    brain_provider=cur.get("brain_provider") or "manual_edit",
                )
            )
            return {"ok": True}

        try:
            await st.set_draft_status(draft_id, req.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/import")
    async def api_import(req: ImportPayload):
        if not req.content.strip():
            raise HTTPException(status_code=400, detail="Пустые данные для импорта")
        st = _state()
        await st.init_db()
        await seed_signal_catalog(st, auto_confirm_free=False)
        cfg = _cfg()
        tag = "discover:minjust_registry" if req.is_registry else "import:custom_list"
        items = parse_import_text(
            req.content, source_tag=tag, default_city=req.default_city.strip()
        )
        rep = await persist_discovered_practices(st, items, config=cfg)
        return rep.as_dict()

    @app.post("/api/pipeline/prepare")
    async def api_prepare_pipeline(req: PipelineActionPayload):
        """Профилировать непроверенные сайты, оценить контакты и подготовить черновики."""
        st = _state()
        await st.init_db()
        cfg = _cfg()
        if req.provider_override in ("claude", "agent", "openai_compatible", "template"):
            cfg.brain.provider = req.provider_override  # type: ignore[assignment]
        env = load_env()

        comp_cnt, obs_cnt, prosp_cnt, _ = await run_profile_stage(st, config=cfg, limit=25)
        brain = Brain(st, brain_config=cfg.brain, env=env)
        scout = Scout(brain=brain, state=st, config=cfg)
        scout_res = await scout.score_pending_prospects(limit=req.limit, rescore_all=False)
        writer = Writer(brain=brain, state=st, config=cfg)
        writer_res = await writer.generate_drafts(min_score=req.min_score, limit=req.limit)
        return {
            "profiled_companies": comp_cnt,
            "profile_observations": obs_cnt,
            "new_prospects": prosp_cnt,
            "scout": scout_res,
            "writer": writer_res,
        }

    @app.get("/api/tasks")
    async def api_tasks(status: str = "pending"):
        st = _state()
        await st.init_db()
        return {"tasks": await st.list_agent_tasks(status=status)}

    @app.post("/api/tasks/{task_id}/apply")
    async def api_task_apply(task_id: str, req: AgentTaskSubmit):
        st = _state()
        await st.init_db()
        cfg = _cfg()
        try:
            res = await apply_agent_task_result(st, cfg, task_id, req.response_json)
            return res
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/export/prospects.csv")
    async def api_export_csv(excel_ru: bool = True):
        st = _state()
        await st.init_db()
        cfg = _cfg()
        _, csv_text = await export_prospects_csv(st, cfg, excel_ru=excel_ru)
        return Response(
            content="\ufeff" + csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="friday_prospects.csv"'},
        )

    @app.get("/api/export/companies.csv")
    async def api_export_companies():
        st = _state()
        await st.init_db()
        cfg = _cfg()
        rows = await build_company_register(st, cfg)
        csv_text = register_to_csv(rows)
        return Response(
            content="\ufeff" + csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="friday_companies.csv"'},
        )

    @app.get("/api/export/dossiers.md")
    async def api_export_dossiers():
        st = _state()
        await st.init_db()
        cfg = _cfg()
        _, md = await export_dossiers_markdown(st, cfg, min_score=0)
        return PlainTextResponse(
            content=md,
            headers={"Content-Disposition": 'attachment; filename="friday_dossiers.md"'},
        )

    @app.get("/app.css")
    async def serve_css():
        return FileResponse(WEB_DIR / "app.css", media_type="text/css; charset=utf-8")

    @app.get("/app.js")
    async def serve_js():
        return FileResponse(WEB_DIR / "app.js", media_type="application/javascript; charset=utf-8")

    @app.get("/")
    async def serve_index():
        return FileResponse(WEB_DIR / "index.html", media_type="text/html; charset=utf-8")

    return app


def start_dashboard(
    *,
    host: str = "127.0.0.1",
    port: int = 5555,
    db_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> None:
    app = create_app(db_path=db_path, config_path=config_path)
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"\n  F.R.I.D.A.Y. Веб-панель запущена: http://{display_host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
