"""Brain — мульти-провайдерный аналитический движок F.R.I.D.A.Y.

Поддерживает 4 режима мышления (`brain.provider` в `friday.yaml`):
1. `claude` (по умолчанию, как у Harvey) — локальный вызов `claude -p` по подписке Claude Max/Pro
   в полной изоляции без инструментов.
2. `agent` — работа на базе самого ИИ-агента рабочей среды (Arena.ai Agent Mode, Cursor, Windsurf,
   Claude Code). Python собирает все факты детерминированно и складывает готовые пакеты задач в
   таблицу `agent_tasks` и папку `data/agent_tasks/`, а агент среды читает их, анализирует и
   применяет ответы через `friday tasks apply`.
3. `openai_compatible` — универсальный HTTP-клиент для любого OpenAI-совместимого API
   (OpenRouter, локальный Ollama/vLLM, Qwen, DeepSeek, шлюзы YandexGPT / GigaChat).
4. `template` — полностью автономный детерминированный режим без LLM (строит оценку и аккуратный
   каркас сообщения исключительно по подтверждённым сигналам).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

from friday.config import BrainConfig, EnvConfig, FridayConfig, load_env
from friday.paths import AGENT_TASKS_DIR, PROMPTS_DIR, SKILLS_DIR
from friday.state import StateManager
from friday.textio import read_user_text

logger = logging.getLogger("friday.brain")

DEFAULT_TIMEOUT_SECONDS = 240.0
DEFAULT_MAX_RETRIES = 2
RETRY_BASE_DELAY = 3.0

SKILL_MAP: dict[str, tuple[str, ...]] = {
    "scout": (
        "fz38_kpea_compliance.md",
        "signal_playbook_ru.md",
        "lead_qualification_ru.md",
    ),
    "writer": (
        "fz38_kpea_compliance.md",
        "signal_playbook_ru.md",
        "outreach_frameworks_ru.md",
    ),
}


class AgentTaskQueued(Exception):
    """В режиме `brain.provider == 'agent'` задача поставлена в очередь для ИИ-агента среды."""

    def __init__(self, task_id: str, file_path: str):
        super().__init__(f"Задача поставлена в очередь ИИ-агента: {task_id} ({file_path})")
        self.task_id = task_id
        self.file_path = file_path


def extract_json_payload(raw_text: str):
    """Извлечь JSON-объект или JSON-массив из ответа модели (устойчиво к ```json обёрткам)."""
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass

    # Поиск первого сбалансированного [...] или {...}
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end > start:
            snippet = cleaned[start : end + 1]
            try:
                return json.loads(snippet)
            except ValueError:
                continue
    return None


class Brain:
    def __init__(
        self,
        state: StateManager,
        brain_config: BrainConfig | None = None,
        env: EnvConfig | None = None,
    ):
        self.state = state
        self.config = brain_config or BrainConfig()
        self.env = env or load_env()
        self.provider = self.config.provider
        self.model = self.config.model
        self.last_reply_empty = False

    def load_prompt(self, name: str, variables: dict[str, str] | None = None) -> str:
        path = PROMPTS_DIR / f"{name}.md"
        if not path.exists():
            return ""
        text = read_user_text(path)
        for key, val in (variables or {}).items():
            text = text.replace("{{" + key + "}}", str(val))
        return text

    def load_skills_for_agent(self, agent_name: str) -> str:
        files = SKILL_MAP.get(agent_name, ())
        chunks: list[str] = []
        for fname in files:
            p = SKILLS_DIR / fname
            if p.exists():
                chunks.append(read_user_text(p).strip())
        local_rules = SKILLS_DIR / "outreach_rules.local.md"
        if agent_name == "writer" and local_rules.exists():
            chunks.append(read_user_text(local_rules).strip())
        return "\n\n---\n\n".join(c for c in chunks if c)

    def build_system_header(self, config: FridayConfig) -> str:
        return self.load_prompt(
            "system",
            {
                "company_name": config.persona.company,
                "bureau_description": config.bureau.description,
            },
        )

    async def preflight(self) -> tuple[bool, str]:
        """Проверить готовность выбранного провайдера без платного вызова модели."""
        if self.provider == "template":
            return True, "Детерминированный режим `template` активен (внешняя LLM не требуется)."
        if self.provider == "agent":
            return True, (
                "Режим `agent` активен: задачи анализа и генерации сохраняются в `data/agent_tasks/` "
                "для выполнения непосредственно ИИ-агентом вашей среды (например, Arena / Cursor)."
            )
        if self.provider == "openai_compatible":
            base_url = self.env.openai_base_url or "https://api.openai.com/v1"
            model = self.model or self.env.openai_model
            if not model:
                return False, "Для `openai_compatible` укажите `brain.model` в friday.yaml или `OPENAI_MODEL` в .env."
            if "localhost" not in base_url and "127.0.0.1" not in base_url and not self.env.openai_api_key:
                return False, "Для внешнего `openai_compatible` шлюза укажите `OPENAI_API_KEY` в .env."
            return True, f"OpenAI-совместимый API готов ({base_url}, модель {model})."

        # Default: claude CLI
        from friday.claude_runtime import ClaudeRuntimeError, check_cli

        try:
            exe = await asyncio.to_thread(check_cli)
            return True, f"Claude CLI готов и изолирован ({exe})."
        except ClaudeRuntimeError as exc:
            return False, str(exc)

    async def queue_for_workspace_agent(
        self,
        *,
        kind: str,
        payload: dict,
        prompt: str,
    ) -> tuple[str, Path]:
        """Сохранить задачу для выполнения самим ИИ-агентом рабочей среды."""
        task_id = await self.state.create_agent_task(kind=kind, payload=payload, prompt_text=prompt)
        AGENT_TASKS_DIR.mkdir(parents=True, exist_ok=True)
        task_file = AGENT_TASKS_DIR / f"{task_id}_{kind}.json"
        packet = {
            "task_id": task_id,
            "kind": kind,
            "instructions": (
                "Прочитайте поле `prompt`, сформируйте строго валидный JSON-ответ и примените его "
                f"командой: friday tasks apply {task_id} --file <путь_к_ответу.json>"
            ),
            "payload": payload,
            "prompt": prompt,
        }
        task_file.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        return task_id, task_file

    async def think(
        self,
        prompt: str,
        *,
        agent: str = "",
        task: str = "",
        task_payload: dict | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> str:
        """Отправить промпт выбранному провайдеру мышления и вернуть текст ответа."""
        self.last_reply_empty = False
        if self.provider == "agent":
            tid, tpath = await self.queue_for_workspace_agent(
                kind=task or agent or "generic",
                payload=task_payload or {},
                prompt=prompt,
            )
            raise AgentTaskQueued(tid, str(tpath))

        if self.provider == "openai_compatible":
            return await self._think_openai_compatible(
                prompt, agent=agent, task=task, timeout=timeout, max_retries=max_retries
            )

        if self.provider == "claude":
            return await self._think_claude(
                prompt, agent=agent, task=task, timeout=timeout, max_retries=max_retries
            )

        self.last_reply_empty = True
        return ""

    async def think_json(
        self,
        prompt: str,
        *,
        agent: str = "",
        task: str = "",
        task_payload: dict | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        """Получить и распарсить JSON-ответ от модели."""
        raw = await self.think(
            prompt, agent=agent, task=task, task_payload=task_payload, timeout=timeout
        )
        if not raw:
            self.last_reply_empty = True
            return None
        return extract_json_payload(raw)

    async def _think_claude(
        self,
        prompt: str,
        *,
        agent: str,
        task: str,
        timeout: float,
        max_retries: int,
    ) -> str:
        from friday import claude_runtime

        try:
            executable = await asyncio.to_thread(claude_runtime.check_cli)
        except claude_runtime.ClaudeRuntimeError as exc:
            logger.error("Вызов Claude отменён: %s", exc)
            self.last_reply_empty = True
            return ""

        cmd = claude_runtime.build_command(executable, self.model)
        for attempt in range(max_retries + 1):
            if attempt > 0:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            t0 = time.monotonic()
            process = None
            try:
                with claude_runtime.isolated_cwd() as cwd:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=cwd,
                        env=claude_runtime.runtime_env(cwd),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(prompt.encode("utf-8")),
                        timeout=timeout,
                    )
                duration_ms = int((time.monotonic() - t0) * 1000)
                if process.returncode != 0:
                    err = stderr.decode("utf-8", errors="replace").strip()
                    logger.error("Claude завершился с кодом %s: %s", process.returncode, err[:300])
                    if any(x in err.lower() for x in ("not logged in", "claude login", "unauthorized")):
                        self.last_reply_empty = True
                        return ""
                    continue

                raw_out = stdout.decode("utf-8", errors="replace").strip()
                text, usage_meta, is_err = self._parse_claude_json(raw_out)
                await self.state.record_usage_event(
                    provider="claude",
                    agent=agent,
                    task=task,
                    model=usage_meta.get("model", self.model or "claude"),
                    input_tokens=int(usage_meta.get("input_tokens", 0)),
                    output_tokens=int(usage_meta.get("output_tokens", 0)),
                    duration_ms=duration_ms,
                    is_error=is_err,
                )
                if is_err:
                    continue
                return text
            except asyncio.TimeoutError:
                logger.error("Таймаут вызова Claude (%.0fs)", timeout)
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
            except Exception as exc:
                logger.error("Ошибка вызова Claude: %s", exc)

        self.last_reply_empty = True
        return ""

    @staticmethod
    def _parse_claude_json(raw: str) -> tuple[str, dict, bool]:
        try:
            payload = json.loads(raw)
        except ValueError:
            return raw, {}, False
        if not isinstance(payload, dict):
            return raw, {}, False
        usage = payload.get("usage") or {}
        meta = {
            "model": payload.get("model", ""),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
        return str(payload.get("result") or ""), meta, bool(payload.get("is_error"))

    async def _think_openai_compatible(
        self,
        prompt: str,
        *,
        agent: str,
        task: str,
        timeout: float,
        max_retries: int,
    ) -> str:
        base_url = (self.env.openai_base_url or "https://api.openai.com/v1").rstrip("/")
        model = self.model or self.env.openai_model
        if not model:
            logger.error("Не задана модель для openai_compatible.")
            self.last_reply_empty = True
            return ""

        headers = {"Content-Type": "application/json"}
        if self.env.openai_api_key:
            headers["Authorization"] = f"Bearer {self.env.openai_api_key}"

        url = f"{base_url}/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                t0 = time.monotonic()
                try:
                    resp = await client.post(url, headers=headers, json=body)
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    if resp.status_code >= 500 or resp.status_code == 429:
                        logger.warning("HTTP %s от LLM API, повтор...", resp.status_code)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    choices = data.get("choices") or []
                    content = (choices[0].get("message") or {}).get("content", "") if choices else ""
                    usage = data.get("usage") or {}
                    await self.state.record_usage_event(
                        provider="openai_compatible",
                        agent=agent,
                        task=task,
                        model=model,
                        input_tokens=int(usage.get("prompt_tokens", 0)),
                        output_tokens=int(usage.get("completion_tokens", 0)),
                        duration_ms=duration_ms,
                        is_error=False,
                    )
                    return str(content).strip()
                except Exception as exc:
                    logger.error("Ошибка OpenAI-совместимого вызова: %s", exc)

        self.last_reply_empty = True
        return ""
