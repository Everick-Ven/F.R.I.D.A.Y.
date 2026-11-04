"""Writer — генерация аналитического досье и персональных черновиков для ручного захода.

Никакой автоматической рассылки: каждый черновик проходит детерминированный `gate.check_draft`
(проверка 38-ФЗ «О рекламе», ст. 17 КПЭА, стоп-листа и анти-спама) и попадает в очередь
`pending_review` для просмотра и ручной отправки человеком.
"""

from __future__ import annotations

import json
import logging

from friday.brain import AgentTaskQueued, Brain
from friday.config import FridayConfig
from friday.fit import pick_primary_signal
from friday.gate import check_draft, match_do_not_contact
from friday.models import Company, Draft, Prospect
from friday.state import StateManager
from friday.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("friday.writer")


class Writer:
    def __init__(self, brain: Brain, state: StateManager, config: FridayConfig):
        self.brain = brain
        self.state = state
        self.config = config

    async def generate_drafts(
        self,
        *,
        min_score: int = 45,
        limit: int = 10,
        channels: list[str] | None = None,
    ) -> dict:
        """Создать черновики для лучших оценённых контактов со статусом `new`."""
        target_channels = channels or list(self.config.bureau.primary_channels) or ["telegram", "email"]
        prospects = await self.state.get_prospects(status="new", min_score=min_score, limit=limit * 2)

        created = 0
        blocked = 0
        queued_tasks: list[str] = []
        dnc = self.config.compliance.do_not_contact

        selected: list[tuple[Prospect, Company | None, list[dict]]] = []
        for p in prospects:
            comp = await self.state.get_company(p.company_id) if p.company_id else None
            if match_do_not_contact(
                domain=(comp.domain if comp else ""),
                email=p.email,
                telegram=p.telegram or (comp.telegram if comp else ""),
                entries=dnc,
            ):
                continue
            obs = await self.state.get_observations(company_id=p.company_id) if p.company_id else []
            selected.append((p, comp, obs))
            if len(selected) >= limit:
                break

        for p, comp, obs in selected:
            sig_code, sig_desc = pick_primary_signal(obs)

            if self.brain.provider == "template":
                result_obj = build_template_draft_bundle(
                    prospect=p,
                    company=comp,
                    observations=obs,
                    sig_code=sig_code,
                    sig_desc=sig_desc,
                    config=self.config,
                )
                c_ok, c_blk = await apply_writer_response(
                    self.state,
                    self.config,
                    prospect=p,
                    company=comp,
                    payload=result_obj,
                    channels=target_channels,
                    provider_label="template",
                )
                created += c_ok
                blocked += c_blk
                continue

            prompt, task_meta = self._build_writer_prompt(
                prospect=p,
                company=comp,
                observations=obs,
                sig_code=sig_code,
                sig_desc=sig_desc,
                channels=target_channels,
            )
            try:
                parsed = await self.brain.think_json(
                    prompt,
                    agent="writer",
                    task="writer_draft",
                    task_payload=task_meta,
                )
            except AgentTaskQueued as queued:
                queued_tasks.append(queued.task_id)
                continue

            if not isinstance(parsed, dict):
                logger.warning("Writer: некорректный JSON для %s, пропуск.", p.id)
                continue

            c_ok, c_blk = await apply_writer_response(
                self.state,
                self.config,
                prospect=p,
                company=comp,
                payload=parsed,
                channels=target_channels,
                provider_label=self.brain.provider,
            )
            created += c_ok
            blocked += c_blk

        return {
            "drafts_ready": created,
            "drafts_blocked_by_gate": blocked,
            "queued_task_ids": queued_tasks,
            "mode": self.brain.provider,
        }

    def _build_writer_prompt(
        self,
        *,
        prospect: Prospect,
        company: Company | None,
        observations: list[dict],
        sig_code: str,
        sig_desc: str,
        channels: list[str],
    ) -> tuple[str, dict]:
        sys_hdr = self.brain.build_system_header(self.config)
        skills = self.brain.load_skills_for_agent("writer")
        principles_md = "\n".join(f"- {item}" for item in self.config.bureau.principles)
        writer_rules = self.brain.load_prompt(
            "writer",
            {
                "persona_name": self.config.persona.name,
                "persona_role": self.config.persona.role,
                "persona_company": self.config.persona.company,
                "persona_tone": self.config.persona.tone,
                "bureau_description": self.config.bureau.description,
                "bureau_principles": principles_md,
                "entry_offer": self.config.bureau.entry_offer,
                "max_telegram_words": str(self.config.compliance.max_telegram_words),
                "max_email_words": str(self.config.compliance.max_email_words),
            },
        )

        salutation = prospect.polite_salutation(self.config.persona.address_style)
        facts = {
            "prospect_id": prospect.id,
            "salutation_hint": salutation or "Здравствуйте (без имени, т.к. контакт общий)",
            "full_name": prospect.full_name(),
            "title": prospect.title,
            "registry_number": prospect.registry_number,
            "company_name": company.name if company else prospect.company,
            "legal_form": company.legal_form if company else "",
            "city": company.city if company else prospect.city,
            "website": company.website if company else prospect.source_url,
            "practice_areas": company.practice_areas if company else [],
            "tech_stack": company.tech_stack if company else [],
            "scout_angle": prospect.personalization_notes,
            "primary_signal_code": sig_code,
            "primary_signal_summary": sig_desc,
            "observations": [
                {
                    "code": o["signal_code"],
                    "num": o["value_num"],
                    "text": o["value_text"],
                    "evidence_url": o["evidence_url"],
                }
                for o in observations[:10]
            ],
        }

        schema_spec = {
            "prospect_id": prospect.id,
            "signal_used": sig_code or "PROFILE_OBSERVATION",
            "audit_summary": "Краткая памятка для человека перед ручным контактом (3-4 предложения: чем сильна практика, в чём точка потери в Яндексе/картах, куда лучше написать).",
            "drafts": {
                ch: (
                    {"subject": "спокойная тема 2-4 слова", "body": "текст письма"}
                    if ch == "email"
                    else {"subject": "", "body": "короткое сообщение без ссылок и без восклицательных знаков"}
                )
                for ch in channels
            },
        }

        prompt = (
            f"{sys_hdr}\n\n{UNTRUSTED_RULE}\n\n"
            f"=== НАВЫКИ ===\n{skills}\n\n"
            f"=== ПРАВИЛА WRITER ===\n{writer_rules}\n\n"
            f"=== ФАКТЫ О ПРАКТИКЕ И АДВОКАТЕ ===\n"
            f"{fence(json.dumps(facts, ensure_ascii=False, indent=2), 'prospect_facts')}\n\n"
            f"Подготовьте черновики для каналов: {', '.join(channels)}.\n"
            f"Верните СТРОГО JSON-объект по образцу:\n"
            f"{json.dumps(schema_spec, ensure_ascii=False, indent=2)}"
        )
        return prompt, {"prospect_id": prospect.id, "channels": channels}


def build_template_draft_bundle(
    *,
    prospect: Prospect,
    company: Company | None,
    observations: list[dict],
    sig_code: str,
    sig_desc: str,
    config: FridayConfig,
) -> dict:
    """Построить аккуратный русскоязычный каркас досье и черновиков строго по фактам (без LLM)."""
    salutation = prospect.polite_salutation(config.persona.address_style)
    greeting = f"{salutation}, здравствуйте." if salutation else "Здравствуйте."
    comp_name = (company.name if company else prospect.company) or "вашей практики"
    city = (company.city if company else prospect.city) or ""
    city_phrase = f" в городе {city}" if city else ""

    if sig_desc:
        obs_sentence = f"Изучал видимость адвокатских практик{city_phrase} и обратил внимание: {sig_desc}."
    else:
        obs_sentence = (
            f"Посмотрел сайт и карточки «{comp_name}»{city_phrase} по профильным запросам доверителей в Яндексе."
        )

    tg_body = (
        f"{greeting}\n\n"
        f"{obs_sentence}\n\n"
        f"Мы профильно выстраиваем привлечение доверителей для адвокатов и коллегий по правилам "
        f"ст. 17 КПЭА и ФЗ «О рекламе», все кабинеты и аналитика остаются на стороне доверителя. "
        f"Подскажите, задача по системной загрузке практики сейчас для вас актуальна?"
    )

    email_subject = f"видимость практики в яндексе"
    email_body = (
        f"{greeting}\n\n"
        f"{obs_sentence}\n\n"
        f"Мы работаем с адвокатскими бюро и коллегиями: настраиваем поиск Яндекса, Директ и карточки "
        f"в Яндекс Картах и 2ГИС строго в рамках ст. 17 КПЭА и 38-ФЗ, без пустых обещаний и с оформлением "
        f"всех доступов на доверителя.\n\n"
        f"Если тема привлечения доверителей актуальна, могу прислать сюда короткий текстовый разбор "
        f"из 5 пунктов по вашей практике без созвонов и презентаций. Прислать?"
    )

    codes_list = ", ".join(sorted({o["signal_code"] for o in observations})) or "базовый профиль"
    rec_channel = (
        f"Telegram ({prospect.telegram or (company.telegram if company else '')})"
        if (prospect.telegram or (company and company.telegram))
        else (f"Email ({prospect.email})" if prospect.email else "форма на сайте / телефон")
    )
    audit_summary = (
        f"Практика: {comp_name} ({(company.legal_form if company else '') or 'форма не уточнена'}, {city or 'регион РФ'}).\n"
        f"Контакт: {prospect.full_name() or 'Общий контакт'} ({prospect.title or 'Адвокат'}).\n"
        f"Главный сигнал: {sig_desc or 'профильное соответствие ICP'} [всего сигналов: {codes_list}].\n"
        f"Рекомендуемый ручной канал: {rec_channel}."
    )

    return {
        "prospect_id": prospect.id,
        "signal_used": sig_code or "ICP_MATCH",
        "audit_summary": audit_summary,
        "drafts": {
            "telegram": {"subject": "", "body": tg_body},
            "tenchat": {"subject": "", "body": tg_body},
            "email": {"subject": email_subject, "body": email_body},
        },
    }


async def apply_writer_response(
    state: StateManager,
    config: FridayConfig,
    *,
    prospect: Prospect,
    company: Company | None,
    payload: dict,
    channels: list[str],
    provider_label: str,
) -> tuple[int, int]:
    """Проверить каждый черновик через `gate.check_draft` и сохранить в таблицу `drafts`."""
    drafts_map = payload.get("drafts") or {}
    audit_summary = str(payload.get("audit_summary") or "").strip()
    signal_used = str(payload.get("signal_used") or "").strip()

    ready = 0
    blocked = 0
    for ch in channels:
        item = drafts_map.get(ch)
        if not isinstance(item, dict):
            continue
        subj = str(item.get("subject") or "").strip()
        body = str(item.get("body") or "").strip()
        if not body:
            continue

        gate_res = check_draft(
            channel=ch,
            subject=subj,
            body=body,
            config=config,
            domain=(company.domain if company else ""),
            email=prospect.email,
            telegram=prospect.telegram or (company.telegram if company else ""),
        )
        status = "pending_review" if gate_res.ok else "gate_blocked"
        notes = [*gate_res.reasons, *(f"предупреждение: {w}" for w in gate_res.warnings)]

        await state.upsert_draft(
            Draft(
                prospect_id=prospect.id,
                company_id=prospect.company_id,
                channel=ch,
                subject=subj,
                body=body,
                audit_summary=audit_summary,
                signal_used=signal_used,
                status=status,
                gate_notes=notes,
                brain_provider=provider_label,
            )
        )
        if gate_res.ok:
            ready += 1
        else:
            blocked += 1

    if ready > 0:
        await state.set_prospect_status(prospect.id, "drafted")

    return ready, blocked
