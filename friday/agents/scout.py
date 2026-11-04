"""Scout — скоринг адвокатских образований и формулировка персонального угла захода."""

from __future__ import annotations

import json
import logging

from friday.brain import AgentTaskQueued, Brain
from friday.config import FridayConfig
from friday.fit import company_fit, pick_primary_signal
from friday.models import Prospect
from friday.state import StateManager
from friday.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("friday.scout")

BATCH_SIZE = 12


class Scout:
    def __init__(self, brain: Brain, state: StateManager, config: FridayConfig):
        self.brain = brain
        self.state = state
        self.config = config

    async def score_pending_prospects(
        self,
        *,
        limit: int = 30,
        rescore_all: bool = False,
    ) -> dict:
        """Оценить контакты и сформировать заметку персонализации по собранным сигналам."""
        prospects = await self.state.get_prospects(status="new", limit=limit * 2)
        if not rescore_all:
            prospects = [p for p in prospects if p.score == 0 or not p.personalization_notes]
        prospects = prospects[:limit]

        if not prospects:
            return {"scored": 0, "queued_task_ids": []}

        # Собираем контекст по компаниям и их наблюдениям
        items_context: list[dict] = []
        for p in prospects:
            comp = await self.state.get_company(p.company_id) if p.company_id else None
            obs = await self.state.get_observations(company_id=p.company_id) if p.company_id else []
            comp_dict = comp.__dict__ if comp else {"name": p.company, "city": p.city}
            det_score, det_reasons = company_fit(comp_dict, [p.__dict__], obs, self.config)
            sig_code, sig_desc = pick_primary_signal(obs)
            items_context.append(
                {
                    "prospect": p,
                    "company": comp,
                    "observations": obs,
                    "det_score": det_score,
                    "det_reasons": det_reasons,
                    "primary_signal_code": sig_code,
                    "primary_signal_desc": sig_desc,
                }
            )

        # В детерминированном режиме `template` сразу считаем по формуле и сигналам без LLM
        if self.brain.provider == "template":
            scored = 0
            for ctx in items_context:
                p: Prospect = ctx["prospect"]
                note = _build_template_angle(ctx)
                await self.state.update_prospect_scoring(
                    p.id, score=max(1, ctx["det_score"]), personalization_notes=note
                )
                scored += 1
            return {"scored": scored, "queued_task_ids": [], "mode": "template"}

        scored_total = 0
        queued_tasks: list[str] = []

        for start in range(0, len(items_context), BATCH_SIZE):
            batch = items_context[start : start + BATCH_SIZE]
            prompt, payload = self._build_scout_prompt(batch)
            try:
                parsed = await self.brain.think_json(
                    prompt,
                    agent="scout",
                    task="scout_score",
                    task_payload=payload,
                )
            except AgentTaskQueued as queued:
                logger.info("Scout: пакет передан в очередь ИИ-агента (%s)", queued.file_path)
                queued_tasks.append(queued.task_id)
                continue

            if isinstance(parsed, list):
                scored_total += await apply_scout_response(self.state, parsed)
            else:
                # Надёжный откат к детерминированной оценке, если модель не ответила
                logger.warning("Scout: LLM не вернула валидный JSON, применяем расчёт по сигналам.")
                for ctx in batch:
                    p = ctx["prospect"]
                    await self.state.update_prospect_scoring(
                        p.id,
                        score=max(1, ctx["det_score"]),
                        personalization_notes=_build_template_angle(ctx),
                    )
                    scored_total += 1

        return {"scored": scored_total, "queued_task_ids": queued_tasks, "mode": self.brain.provider}

    def _build_scout_prompt(self, batch: list[dict]) -> tuple[str, dict]:
        sys_hdr = self.brain.build_system_header(self.config)
        skills = self.brain.load_skills_for_agent("scout")
        scout_instructions = self.brain.load_prompt(
            "scout",
            {
                "legal_forms": ", ".join(self.config.icp.legal_forms),
                "practice_areas": ", ".join(self.config.icp.practice_areas),
                "cities": ", ".join(self.config.icp.cities),
                "titles": ", ".join(self.config.icp.decision_maker_titles),
            },
        )

        serialized_leads = []
        for ctx in batch:
            p: Prospect = ctx["prospect"]
            c = ctx["company"]
            obs_short = [
                {
                    "code": o["signal_code"],
                    "num": o["value_num"],
                    "text": o["value_text"],
                    "evidence_url": o["evidence_url"],
                }
                for o in ctx["observations"][:12]
            ]
            serialized_leads.append(
                {
                    "prospect_id": p.id,
                    "fio": p.full_name() or "Общий контакт",
                    "title": p.title,
                    "registry_number": p.registry_number,
                    "company_name": c.name if c else p.company,
                    "legal_form": c.legal_form if c else "",
                    "city": (c.city if c else p.city),
                    "website": c.website if c else p.source_url,
                    "practice_areas": c.practice_areas if c else [],
                    "tech_stack": c.tech_stack if c else [],
                    "channels": {
                        "telegram": p.telegram or (c.telegram if c else ""),
                        "email": p.email,
                        "phone": p.phone or (c.phone if c else ""),
                    },
                    "confirmed_observations": obs_short,
                    "baseline_fit_hint": ctx["det_score"],
                }
            )

        schema_hint = (
            'Верните строго JSON-массив вида:\n'
            '[{"prospect_id": "...", "score": 82, "angle": "Конкретное наблюдение в 1-2 предложениях"}]'
        )
        full_prompt = (
            f"{sys_hdr}\n\n{UNTRUSTED_RULE}\n\n"
            f"=== НАВЫКИ ===\n{skills}\n\n"
            f"=== ЗАДАЧА SCOUT ===\n{scout_instructions}\n\n"
            f"=== ДАННЫЕ ПРАКТИК ===\n"
            f"{fence(json.dumps(serialized_leads, ensure_ascii=False, indent=2), 'prospect_batch')}\n\n"
            f"{schema_hint}"
        )
        return full_prompt, {"prospect_ids": [x["prospect"].id for x in batch]}


def _build_template_angle(ctx: dict) -> str:
    c = ctx["company"]
    p: Prospect = ctx["prospect"]
    sig_desc = ctx.get("primary_signal_desc") or ""
    comp_name = (c.name if c else p.company) or "практики"
    city = (c.city if c else p.city) or ""
    loc_part = f" ({city})" if city else ""
    if sig_desc:
        return f"У «{comp_name}»{loc_part} зафиксирован сигнал: {sig_desc}."
    return f"Практика «{comp_name}»{loc_part}, роль контакта: {p.title or 'Адвокат'}."


async def apply_scout_response(state: StateManager, items: list[dict]) -> int:
    """Применить JSON-массив оценок Scout (из LLM или из `friday tasks apply`)."""
    applied = 0
    for row in items:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("prospect_id") or "").strip()
        if not pid:
            continue
        try:
            score = int(row.get("score") or 50)
        except (ValueError, TypeError):
            score = 50
        angle = str(row.get("angle") or row.get("personalization_notes") or "").strip()
        await state.update_prospect_scoring(pid, score=score, personalization_notes=angle)
        applied += 1
    return applied
