"""Мозг F.R.I.D.A.Y. — полная архитектура Harvey без шаблонов:
1. `api`    — прямой вызов LLM API (OpenRouter, OpenAI, Anthropic, DeepSeek, локальный Ollama и др.);
2. `claude` — локальный изолированный Claude Code CLI (`--safe-mode --tools ""` без истории и MCP);
3. `codex`  — локальный изолированный Codex CLI (`--sandbox read-only`);
4. `agent`  — очередь типизированных задач `agent_tasks` для ИИ-агента рабочей среды (Arena, Claude Code, Cursor).

Никаких зашитых текстовых шаблонов в Python нет: каждое письмо создаётся индивидуально по проверенным
фактам (`<untrusted>`), системным правилам `prompts/writer.md`, базам знаний `skills/*.md`, опциональному
проходу `humanize.py` и проходит обязательный детерминированный шлюз `friday/gate.py` (ФЗ-38 / КПЭА).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from friday.claude_runtime import (
    CliRuntimeError,
    check_claude_cli,
    check_codex_cli,
    run_claude_isolated,
    run_codex_isolated,
)
from friday.config import FridayConfig, load_config, load_env
from friday.gate import validate_draft
from friday.models.draft import Draft
from friday.paths import DATA_DIR, PROMPTS_DIR, SKILLS_DIR
from friday.state import StateManager
from friday.trainer import apply_niche_setup

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

SETTING_LLM_API_KEY = "llm_api_key"

PROVIDER_LABELS = {
    "api": "LLM API",
    "claude": "Claude Code CLI",
    "codex": "Codex CLI",
    "agent": "ИИ-агент рабочей среды",
}


class BrainError(RuntimeError):
    """Ошибка вызова или разбора ответа Мозга."""


class BrainUnavailableError(BrainError):
    """Выбранный провайдер Мозга недоступен или требует настройки."""


class AgentTaskQueued(Exception):
    """Задача поставлена в очередь для ИИ-агента рабочей среды."""

    def __init__(self, task_id: str, kind: str, task_file: str = ""):
        super().__init__(f"Создана задача ИИ-агента #{task_id} ({kind})")
        self.task_id = task_id
        self.kind = kind
        self.task_file = task_file


def load_prompt(name: str, **variables: str) -> str:
    """Читает `prompts/<name>.md` и подставляет переменные конфигурации `{{...}}`."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8").strip()
    if not variables:
        return raw

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return str(variables[key]) if key in variables else match.group(0)

    return TEMPLATE_VAR_RE.sub(_replace, raw)


def load_skills(names: list[str] | None = None) -> str:
    """Собирает активные базы знаний из `skills/*.md` (включая обученный `product_knowledge.md`)."""
    if not SKILLS_DIR.exists():
        return ""
    chunks: list[str] = []
    excluded = {"README.md", "email_rules.md", "outreach_rules.local.md", "humanizer.md"}
    if names is None:
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
            if p.name not in excluded and p.name not in seen:
                ordered_files.append(p)
        files = ordered_files
    else:
        files = [SKILLS_DIR / f"{n}.md" for n in names if (SKILLS_DIR / f"{n}.md").exists()]

    for f in files:
        chunks.append(f"### Навык: {f.stem}\n\n{f.read_text(encoding='utf-8').strip()}")
    return "\n\n---\n\n".join(chunks)


def extract_json_object(raw: str | dict) -> dict:
    """Извлекает JSON-объект из ответа модели (поддерживает чистый JSON и markdown-блоки)."""
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip()
    if not text:
        raise BrainError("Пустой JSON-ответ от модели")
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


def mask_api_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 10:
        return k[:2] + "•••"
    return f"{k[:6]}•••{k[-4:]}"


async def get_configured_api_key(state: StateManager | None = None) -> str:
    if state is not None:
        stored = (await state.get_setting(SETTING_LLM_API_KEY)).strip()
        if stored:
            return stored
    return load_env().effective_llm_key()


def _is_local_unauthenticated_api(base_url: str) -> bool:
    low = (base_url or "").strip().lower()
    return "://localhost" in low or "://127.0.0.1" in low


class Brain:
    """Движок мышления F.R.I.D.A.Y. по схеме Harvey (`think` / `think_json` + preflight)."""

    def __init__(
        self,
        state: StateManager | None = None,
        config: FridayConfig | None = None,
        *,
        api_key_override: str = "",
    ):
        self.state = state
        self.config = config or load_config()
        self._api_key_override = api_key_override.strip()

    async def get_api_key(self) -> str:
        if self._api_key_override:
            return self._api_key_override
        return await get_configured_api_key(self.state)

    async def inspect_status(self) -> dict:
        """Возвращает детальную диагностику доступных рантаймов Мозга для UI и CLI."""
        cfg_b = self.config.brain
        api_key = await self.get_api_key()
        has_api = bool(api_key) or _is_local_unauthenticated_api(cfg_b.api_base_url)

        claude_ok = False
        claude_reason = ""
        try:
            await asyncio.to_thread(check_claude_cli)
            claude_ok = True
        except CliRuntimeError as exc:
            claude_reason = str(exc)

        codex_ok = False
        codex_reason = ""
        try:
            await asyncio.to_thread(check_codex_cli)
            codex_ok = True
        except CliRuntimeError as exc:
            codex_reason = str(exc)

        selected = cfg_b.provider
        if selected == "auto":
            if has_api:
                effective = "api"
            elif claude_ok:
                effective = "claude"
            elif codex_ok:
                effective = "codex"
            else:
                effective = "agent"
        else:
            effective = selected

        ready_direct = (
            (effective == "api" and has_api)
            or (effective == "claude" and claude_ok)
            or (effective == "codex" and codex_ok)
        )

        return {
            "configured_provider": selected,
            "effective_provider": effective,
            "effective_label": PROVIDER_LABELS.get(effective, effective),
            "direct_inference_ready": ready_direct,
            "api_base_url": cfg_b.api_base_url,
            "model": cfg_b.model,
            "reasoning_effort": cfg_b.reasoning_effort,
            "has_api_key": bool(api_key),
            "masked_api_key": mask_api_key(api_key),
            "humanizer": bool(self.config.writer.humanizer),
            "runtimes": {
                "api": {"available": has_api},
                "claude": {"available": claude_ok, "reason": claude_reason},
                "codex": {"available": codex_ok, "reason": codex_reason},
                "agent": {"available": True},
            },
        }

    @property
    def provider_tag(self) -> str:
        b = self.config.brain
        if b.provider in ("claude", "codex", "agent"):
            return b.provider
        return f"api:{b.model}" if b.model else "api"

    async def _call_http_llm(self, prompt: str, *, timeout: float = 90.0) -> str:
        b = self.config.brain
        base = (b.api_base_url or "https://openrouter.ai/api/v1").strip().rstrip("/")
        api_key = await self.get_api_key()
        if not api_key and not _is_local_unauthenticated_api(base):
            raise BrainUnavailableError(
                "Не задан API-ключ LLM. Укажите ключ OpenRouter / OpenAI / DeepSeek в настройках «Мозг (LLM)»."
            )

        # Поддержка нативного Anthropic Messages API (`api.anthropic.com`)
        if "api.anthropic.com" in base.lower():
            url = base if base.endswith("/messages") else f"{base}/messages"
            if "/v1/" not in url and not url.endswith("/v1/messages"):
                url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            model_name = b.model.split("/", 1)[-1] if "/" in b.model else (b.model or "claude-3-5-sonnet-latest")
            payload = {
                "model": model_name,
                "max_tokens": 1024,
                "temperature": 0.55,
                "messages": [{"role": "user", "content": prompt}],
            }
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise BrainError(f"Ошибка Anthropic API ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            blocks = data.get("content") or []
            return "".join(bl.get("text", "") for bl in blocks if isinstance(bl, dict)).strip()

        # Стандартный OpenAI-совместимый эндпоинт (OpenRouter, OpenAI, DeepSeek, Qwen, Ollama, vLLM)
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if "openrouter.ai" in url.lower():
            headers["HTTP-Referer"] = "https://github.com/Everick-Ven/F.R.I.D.A.Y."
            headers["X-Title"] = "F.R.I.D.A.Y."

        payload = {
            "model": b.model or "anthropic/claude-3.5-sonnet",
            "temperature": 0.55,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise BrainError(f"Ошибка LLM API ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise BrainError(f"Пустой массив choices от LLM API: {str(data)[:240]}")
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "").strip()

    async def think(
        self,
        prompt: str,
        *,
        timeout: float = 120.0,
        task: str = "",
    ) -> tuple[str, str]:
        """Выполняет прямой вызов активной модели и возвращает `(raw_text, provider_used)`."""
        info = await self.inspect_status()
        eff = info["effective_provider"]

        if eff == "api":
            text = await self._call_http_llm(prompt, timeout=timeout)
            return text, f"api:{self.config.brain.model}"
        if eff == "claude":
            if not info["runtimes"]["claude"]["available"]:
                raise BrainUnavailableError(info["runtimes"]["claude"]["reason"])
            text = await asyncio.to_thread(
                run_claude_isolated,
                prompt,
                model=self.config.brain.model,
                timeout=timeout,
            )
            return text, "claude"
        if eff == "codex":
            if not info["runtimes"]["codex"]["available"]:
                raise BrainUnavailableError(info["runtimes"]["codex"]["reason"])
            text = await asyncio.to_thread(
                run_codex_isolated,
                prompt,
                model=self.config.brain.model,
                reasoning_effort=self.config.brain.reasoning_effort,
                timeout=timeout,
            )
            return text, "codex"

        raise BrainUnavailableError(
            "Прямой провайдер LLM (API-ключ или локальный Claude/Codex CLI) не настроен; активен режим очереди ИИ-агента."
        )

    async def think_json(
        self,
        prompt: str,
        *,
        timeout: float = 120.0,
        task: str = "",
    ) -> dict:
        raw, _ = await self.think(prompt, timeout=timeout, task=task)
        return extract_json_object(raw)


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
    if system_prompt.strip():
        full_prompt = (
            f"# СИСТЕМНАЯ ИНСТРУКЦИЯ F.R.I.D.A.Y.\n\n{system_prompt.strip()}\n\n"
            f"---\n\n# ЗАДАЧА ({kind})\n\n{user_prompt.strip()}\n"
        )
    else:
        full_prompt = user_prompt.strip() + "\n"

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
    """Применяет JSON-ответ ИИ-агента рабочей среды к состоянию F.R.I.D.A.Y."""
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
