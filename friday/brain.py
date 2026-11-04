"""Мозг F.R.I.D.A.Y. — Вариант А: ИИ-агент рабочей среды как Мозг (`agent` mode).

Архитектурный принцип:
- F.R.I.D.A.Y. НЕ требует платных API-ключей к внешним LLM и НЕ вызывает сторонние CLI.
- Python-ядро детерминированно собирает данные (Яндекс, 2ГИС, hh.ru, сайты компаний),
  изолирует внешний текст в `<untrusted>`, проверяет юридический шлюз ФЗ-38 (`gate.py`)
  и формирует структурированные пакеты задач (`agent_tasks`) для ИИ-агента рабочей среды
  (Arena.ai Agent Mode, Cursor, Windsurf, Claude Code и др.).
- ИИ-агент рабочей среды читает задачу через `friday tasks next` (или `data/agent_tasks/`),
  выполняет интеллектуальную работу (`train_niche`, `scout_score`, `writer_draft`)
  и записывает результат через `friday tasks apply <task_id> '<json>'` (или REST API).
- Дополнительно доступен режим `template` — мгновенный детерминированный предпросмотр
  черновиков и скоринга без ожидания ответа агента.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from friday.config import FridayConfig, load_config
from friday.gate import validate_draft
from friday.models.draft import Draft
from friday.paths import DATA_DIR, PROMPTS_DIR, SKILLS_DIR
from friday.state import StateManager
from friday.trainer import apply_niche_setup

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class BrainError(RuntimeError):
    """Ошибка обработки задачи Мозга."""


class AgentTaskQueued(Exception):
    """Сигнал о том, что задача поставлена в очередь для ИИ-агента рабочей среды."""

    def __init__(self, task_id: str, kind: str, task_file: str = ""):
        super().__init__(f"Создана задача ИИ-агента #{task_id} ({kind})")
        self.task_id = task_id
        self.kind = kind
        self.task_file = task_file


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_skills(names: list[str] | None = None) -> str:
    """Собирает активные базы знаний из `skills/*.md` (включая обученный `product_knowledge.md`)."""
    if not SKILLS_DIR.exists():
        return ""
    chunks: list[str] = []
    if names is None:
        # Порядок важен: сначала обученный продукт и конкуренты, затем универсальные плейбуки РФ
        priority = [
            "product_knowledge",
            "competitive_intel",
            "ru_compliance",
            "signal_playbook_ru",
            "lead_qualification_ru",
            "outreach_frameworks_ru",
        ]
        seen: set[str] = set()
        ordered_files: list[Path] = []
        for stem in priority:
            p = SKILLS_DIR / f"{stem}.md"
            if p.exists():
                ordered_files.append(p)
                seen.add(p.name)
        for p in sorted(SKILLS_DIR.glob("*.md")):
            if p.name != "README.md" and p.name not in seen:
                ordered_files.append(p)
        files = ordered_files
    else:
        files = [SKILLS_DIR / f"{n}.md" for n in names if (SKILLS_DIR / f"{n}.md").exists()]

    for f in files:
        chunks.append(f"### Навык: {f.stem}\n\n{f.read_text(encoding='utf-8').strip()}")
    return "\n\n---\n\n".join(chunks)


def extract_json_object(raw: str | dict) -> dict:
    """Извлекает JSON-объект из ответа ИИ-агента (поддерживает чистый JSON и markdown-блоки)."""
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip()
    if not text:
        raise BrainError("Пустой JSON-ответ")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass

    m = JSON_BLOCK_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except ValueError as exc:
            raise BrainError(f"Не удалось разобрать JSON ответа: {exc}") from exc

    raise BrainError(f"Ответ не содержит валидного JSON-объекта: {text[:240]}")


def _tasks_dir(cfg: FridayConfig) -> Path:
    raw = (cfg.brain.tasks_dir or "data/agent_tasks").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = DATA_DIR.parent / p
    p.mkdir(parents=True, exist_ok=True)
    return p


async def enqueue_agent_task(
    state: StateManager,
    cfg: FridayConfig,
    *,
    kind: str,
    payload: dict,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str]:
    """Создаёт задачу для ИИ-агента рабочей среды в SQLite и в `data/agent_tasks/`."""
    full_prompt = (
        f"# СИСТЕМНАЯ ИНСТРУКЦИЯ F.R.I.D.A.Y.\n\n{system_prompt.strip()}\n\n"
        f"---\n\n# ЗАДАЧА ({kind})\n\n{user_prompt.strip()}\n"
    )
    tid = await state.create_agent_task(
        kind=kind,
        payload=payload,
        prompt_text=full_prompt,
    )
    tdir = _tasks_dir(cfg)
    task_file = tdir / f"{tid}_{kind}.json"
    packet = {
        "task_id": tid,
        "kind": kind,
        "status": "pending",
        "how_to_apply_cli": f"friday tasks apply {tid} '<json>'",
        "payload": payload,
        "prompt": full_prompt,
    }
    task_file.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    return tid, str(task_file)


async def apply_agent_task_result(
    state: StateManager,
    task_id: str,
    raw_result: str | dict,
    cfg: FridayConfig | None = None,
) -> dict:
    """Применяет JSON-ответ ИИ-агента рабочей среды к состоянию F.R.I.D.A.Y.

    Поддерживаемые типы задач (`kind`):
    - `train_niche`  -> обновляет `friday.local.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`
    - `scout_score`  -> обновляет `score` и `personalization_notes` у ЛПР
    - `writer_draft` -> прогоняет текст через детерминированный шлюз `gate.py` (ФЗ-38) и сохраняет черновик
    """
    cfg = cfg or load_config()
    tasks = await state.list_agent_tasks(status="all")
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        raise BrainError(f"Задача #{task_id} не найдена в очереди agent_tasks")

    parsed = extract_json_object(raw_result)
    kind = task["kind"]
    payload = task.get("payload") or {}
    outcome: dict = {"task_id": task_id, "kind": kind}

    if kind == "train_niche":
        applied = apply_niche_setup(parsed, base_config=cfg)
        outcome["setup"] = applied

    elif kind == "scout_score":
        prospect_id = str(payload.get("prospect_id") or parsed.get("prospect_id") or "")
        if not prospect_id:
            raise BrainError("В задаче scout_score отсутствует prospect_id")
        base_score = int(payload.get("deterministic_score") or 50)
        ai_score = int(parsed.get("score", base_score))
        blended = max(0, min(100, round(0.5 * base_score + 0.5 * ai_score)))
        angle = str(parsed.get("angle") or parsed.get("personalization_notes") or "").strip()
        if not angle:
            raise BrainError("Поле `angle` (угол персонализации) не должно быть пустым")
        await state.update_prospect_scoring(
            prospect_id,
            score=blended,
            personalization_notes=angle,
        )
        outcome.update({"prospect_id": prospect_id, "score": blended, "angle": angle})

    elif kind == "writer_draft":
        prospect_id = str(payload.get("prospect_id") or parsed.get("prospect_id") or "")
        channel = str(payload.get("channel") or parsed.get("channel") or "telegram")
        prospect = await state.get_prospect(prospect_id)
        if not prospect:
            raise BrainError(f"ЛПР {prospect_id} не найден")
        company = await state.get_company(prospect.company_id) if prospect.company_id else None

        subject = str(parsed.get("subject") or "").strip()
        body = str(parsed.get("body") or "").strip()
        audit_summary = str(parsed.get("audit_summary") or "").strip()
        signal_used = str(parsed.get("signal_used") or "").strip()
        if not body:
            raise BrainError("Поле `body` (текст черновика) не должно быть пустым")

        gate_res = validate_draft(
            channel=channel,
            subject=subject,
            body=body,
            company_domain=company.domain if company else "",
            prospect_email=prospect.email,
            prospect_telegram=prospect.telegram or (company.telegram if company else ""),
            config=cfg,
        )
        status = "pending_review" if gate_res.passed else "gate_blocked"
        notes = [*gate_res.reasons, *gate_res.warnings]

        draft = Draft(
            prospect_id=prospect.id,
            company_id=prospect.company_id,
            channel=channel,
            subject=subject,
            body=body,
            audit_summary=audit_summary,
            signal_used=signal_used,
            status=status,
            gate_notes=notes,
            brain_provider="agent",
        )
        did = await state.upsert_draft(draft)
        if gate_res.passed and prospect.status == "new":
            await state.set_prospect_status(prospect.id, "draft_ready")
        outcome.update(
            {
                "draft_id": did,
                "prospect_id": prospect.id,
                "channel": channel,
                "gate_passed": gate_res.passed,
                "status": status,
                "gate_notes": notes,
            }
        )
    else:
        raise BrainError(f"Неизвестный тип задачи: {kind}")

    raw_json_str = json.dumps(parsed, ensure_ascii=False)
    await state.complete_agent_task(task_id, raw_json_str)

    # Обновим JSON-пакет на диске, если он есть
    tfile = _tasks_dir(cfg) / f"{task_id}_{kind}.json"
    if tfile.exists():
        try:
            pkt = json.loads(tfile.read_text(encoding="utf-8"))
            pkt["status"] = "completed"
            pkt["response"] = parsed
            pkt["outcome"] = outcome
            tfile.write_text(json.dumps(pkt, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    return outcome
