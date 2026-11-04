"""Пайплайн разведки и подготовки черновиков: DISCOVER → PROFILE → SCOUT → DRAFT.

Каждая стадия запускается независимо и читает свою очередь задач из SQLite,
а не передаёт списки в памяти:
- `discover` / `import` пополняет реестр адвокатских образований;
- `profile` обходит непроверенные сайты практик ($0, чистый HTTP без LLM);
- `scout` оценивает найденные контакты и формулирует гипотезу по сигналам;
- `writer` создаёт персональное досье и черновики для ручного захода, прогоняя их через `gate`.
Автоматической отправки писем нет архитектурно (ст. 18 ФЗ «О рекламе»).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from friday.agents.scout import Scout, apply_scout_response
from friday.agents.writer import Writer, apply_writer_response
from friday.brain import Brain, extract_json_payload
from friday.collectors.discover import DEFAULT_PROVIDER, DiscoveryQuery, run_discovery
from friday.collectors.profile import profile_companies
from friday.config import EnvConfig, FridayConfig
from friday.state import StateManager

logger = logging.getLogger("friday.pipeline")

PROFILE_STALE_DAYS = 90


@dataclass
class PipelineReport:
    discover: dict | None = None
    profiled_companies: int = 0
    profile_observations: int = 0
    new_prospects_from_sites: int = 0
    scout: dict | None = None
    writer: dict | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "discover": self.discover,
            "profiled_companies": self.profiled_companies,
            "profile_observations": self.profile_observations,
            "new_prospects_from_sites": self.new_prospects_from_sites,
            "scout": self.scout,
            "writer": self.writer,
            "errors": self.errors,
        }


async def run_profile_stage(
    state: StateManager,
    *,
    config: FridayConfig | None = None,
    limit: int = 50,
    stale_days: int = PROFILE_STALE_DAYS,
) -> tuple[int, int, int, str]:
    """Профилировать сайты практик, которые ещё не проверялись (или устарели)."""
    pending = await state.companies_needing_profile(limit=limit, stale_days=stale_days)
    if not pending:
        return 0, 0, 0, ""
    obs_count, new_prospects, run_id = await profile_companies(
        state, pending, config=config
    )
    return len(pending), obs_count, new_prospects, run_id


async def run_full_pipeline(
    state: StateManager,
    config: FridayConfig,
    env: EnvConfig,
    *,
    discover_provider: str | None = None,
    queries: list[DiscoveryQuery] | None = None,
    skip_discover: bool = False,
    profile_limit: int = 50,
    scout_limit: int = 30,
    draft_limit: int = 10,
    min_score_for_draft: int = 45,
) -> PipelineReport:
    """Запустить полный цикл «Разведка → Профилирование → Скоринг → Черновики»."""
    report = PipelineReport()

    if not skip_discover:
        disc = await run_discovery(
            state,
            config,
            env,
            provider_key=discover_provider or DEFAULT_PROVIDER,
            queries=queries,
        )
        report.discover = disc.as_dict()
        report.errors.extend(disc.errors)

    comp_cnt, obs_cnt, prosp_cnt, _ = await run_profile_stage(
        state, config=config, limit=profile_limit
    )
    report.profiled_companies = comp_cnt
    report.profile_observations = obs_cnt
    report.new_prospects_from_sites = prosp_cnt

    brain = Brain(state, brain_config=config.brain, env=env)
    scout = Scout(brain=brain, state=state, config=config)
    report.scout = await scout.score_pending_prospects(limit=scout_limit)

    writer = Writer(brain=brain, state=state, config=config)
    report.writer = await writer.generate_drafts(
        min_score=min_score_for_draft, limit=draft_limit
    )
    return report


async def apply_agent_task_result(
    state: StateManager,
    config: FridayConfig,
    task_id: str,
    raw_response: str,
) -> dict:
    """Применить JSON-ответ от ИИ-агента рабочей среды для задачи `task_id`."""
    tasks = await state.list_agent_tasks(status="all")
    target = next((t for t in tasks if t["id"] == task_id), None)
    if target is None:
        raise ValueError(f"Задача `{task_id}` не найдена в `agent_tasks`.")

    parsed = extract_json_payload(raw_response)
    if parsed is None:
        raise ValueError("Ответ не содержит валидного JSON.")

    kind = target["kind"]
    payload_meta = target.get("payload") or {}

    if kind == "scout_score":
        if not isinstance(parsed, list):
            raise ValueError("Для `scout_score` ожидается JSON-массив оценок.")
        applied = await apply_scout_response(state, parsed)
        await state.complete_agent_task(task_id, json.dumps(parsed, ensure_ascii=False))
        return {"task_id": task_id, "kind": kind, "applied_scores": applied}

    if kind == "writer_draft":
        if not isinstance(parsed, dict):
            raise ValueError("Для `writer_draft` ожидается JSON-объект черновиков.")
        pid = str(parsed.get("prospect_id") or payload_meta.get("prospect_id") or "").strip()
        prospect = await state.get_prospect(pid)
        if not prospect:
            raise ValueError(f"Контакт `{pid}` не найден в БД.")
        company = await state.get_company(prospect.company_id) if prospect.company_id else None
        channels = payload_meta.get("channels") or list(config.bureau.primary_channels)
        ready, blocked = await apply_writer_response(
            state,
            config,
            prospect=prospect,
            company=company,
            payload=parsed,
            channels=channels,
            provider_label="agent",
        )
        await state.complete_agent_task(task_id, json.dumps(parsed, ensure_ascii=False))
        return {
            "task_id": task_id,
            "kind": kind,
            "drafts_ready": ready,
            "drafts_blocked_by_gate": blocked,
        }

    raise ValueError(f"Неизвестный тип задачи агента: {kind}")
