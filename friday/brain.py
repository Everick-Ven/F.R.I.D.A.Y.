"""Мозг F.R.I.D.A.Y. — самонастраивающийся движок (`AutoBrain`) с защитой от пустых ответов LLM API.

Ключевые принципы:
- Пользователь НЕ выбирает вручную модели, провайдеров, температуру или режим Humanizer.
- Система сама определяет лучший контур (`api` из окружения -> `claude` CLI -> `codex` CLI ->
  локальная `ollama` -> браузерный вызов LLM / ИИ-агент рабочей среды).
- Универсальный парсер ответов корректно обрабатывает все современные форматы API 2026 года:
  обычный `choices[0].message.content`, массив мультимодальных блоков `content[]`, рассуждающие модели
  (`o1`/`o3`/`gpt-5`, `deepseek-reasoner`, `GPT-OSS-20B`, `Claude thinking` с `<think>...</think>`),
  OpenAI `/v1/responses`, Anthropic `/v1/messages`, Gemini `candidates[]` и российские OpenAI-совместимые шлюзы.
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
from friday.config import EnvConfig, FridayConfig, load_config, load_env
from friday.gate import validate_draft
from friday.models.draft import Draft
from friday.paths import DATA_DIR, PROMPTS_DIR, SKILLS_DIR
from friday.state import StateManager
from friday.trainer import apply_niche_setup

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
OPENAI_REASONING_MODEL_RE = re.compile(r"(?:^|/)(?:o1|o3|o4|gpt-5)(?:[-.]|$)", re.IGNORECASE)

SETTING_LLM_API_KEY = "llm_api_key"
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

OPENROUTER_FALLBACK_MODELS: tuple[str, ...] = (
    "anthropic/claude-3.5-sonnet",
    "openai/gpt-4o",
    "deepseek/deepseek-chat",
)

PROVIDER_LABELS = {
    "api": "LLM API",
    "claude": "Claude Code CLI",
    "codex": "Codex CLI",
    "ollama": "Ollama Local",
    "agent": "ИИ-агент рабочей среды",
}


class BrainError(RuntimeError):
    """Ошибка вызова или разбора ответа Мозга."""


class BrainUnavailableError(BrainError):
    """Прямой рантайм LLM недоступен в текущем окружении."""


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
    """Собирает активные базы знаний из `skills/*.md`."""
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
        txt = f.read_text(encoding="utf-8").strip()
        if txt:
            chunks.append(f"### Навык: {f.stem}\n\n{txt}")
    return "\n\n---\n\n".join(chunks)


def _flatten_content_blocks(val: object) -> str:
    """Извлекает текст из строки или списка контент-блоков OpenAI / Anthropic / Responses API."""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        text_parts: list[str] = []
        fallback_parts: list[str] = []
        for item in val:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                btype = str(item.get("type") or "").lower()
                if btype in ("thinking", "reasoning"):
                    fb = str(item.get("thinking") or item.get("text") or "").strip()
                    if fb:
                        fallback_parts.append(fb)
                    continue
                t = str(item.get("text") or item.get("content") or item.get("value") or "").strip()
                if t:
                    text_parts.append(t)
        joined = "\n".join(p for p in text_parts if p).strip()
        if joined:
            return joined
        return "\n".join(p for p in fallback_parts if p).strip()
    if isinstance(val, dict):
        return str(val.get("text") or val.get("content") or "").strip()
    return ""


def extract_llm_text_from_payload(data: object) -> str:
    """Универсальное извлечение текста из любого JSON-ответа LLM API (OpenAI, OpenRouter, Anthropic, Gemini, Ollama)."""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return ""

    # 1. OpenAI / OpenRouter / DeepSeek / LiteLLM / Pollinations `choices`
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        msg = c0.get("message") if isinstance(c0.get("message"), dict) else {}
        content_txt = _flatten_content_blocks(msg.get("content"))
        if content_txt:
            return content_txt
        # Некоторые провайдеры кладут текст прямо в `choices[0].text`
        direct_txt = _flatten_content_blocks(c0.get("text"))
        if direct_txt:
            return direct_txt
        # Если рассуждающая модель уложила финальный JSON в `reasoning_content` / `reasoning`
        reasoning_txt = _flatten_content_blocks(msg.get("reasoning_content") or msg.get("reasoning"))
        if reasoning_txt and "{" in reasoning_txt and "}" in reasoning_txt:
            return reasoning_txt

    # 2. OpenAI Responses API (`output_text` или `output[].content[]`)
    out_txt = _flatten_content_blocks(data.get("output_text"))
    if out_txt:
        return out_txt
    output_arr = data.get("output")
    if isinstance(output_arr, list):
        parts: list[str] = []
        for item in output_arr:
            if isinstance(item, dict):
                t = _flatten_content_blocks(item.get("content") or item.get("text"))
                if t:
                    parts.append(t)
        if parts:
            return "\n".join(parts).strip()

    # 3. Anthropic `/v1/messages` (`content: [{"type": "text", "text": "..."}]`)
    anth_txt = _flatten_content_blocks(data.get("content"))
    if anth_txt:
        return anth_txt

    # 4. Google Gemini (`candidates[0].content.parts[].text`)
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        cand_content = candidates[0].get("content")
        if isinstance(cand_content, dict):
            gem_txt = _flatten_content_blocks(cand_content.get("parts"))
            if gem_txt:
                return gem_txt

    # 5. Ollama native (`/api/chat` -> `message.content`, `/api/generate` -> `response`)
    if isinstance(data.get("message"), dict):
        oll_txt = _flatten_content_blocks(data["message"].get("content"))
        if oll_txt:
            return oll_txt
    if isinstance(data.get("response"), str) and data["response"].strip():
        return data["response"].strip()

    return ""


def strip_thinking_blocks(raw: str) -> str:
    """Убирает блоки `<think>...</think>`, чтобы скобки внутри рассуждений не ломали JSON-парсер."""
    cleaned = THINK_BLOCK_RE.sub("", raw or "").strip()
    # Если модель открыла `<think>`, но не закрыла тег перед финальным JSON
    if "<think>" in cleaned.lower():
        idx_close = cleaned.lower().rfind("</think>")
        if idx_close != -1:
            cleaned = cleaned[idx_close + 8 :].strip()
        else:
            last_brace = cleaned.rfind("{")
            if last_brace != -1:
                cleaned = cleaned[last_brace:].strip()
    return cleaned or (raw or "").strip()


def extract_json_object(raw: str | dict) -> dict:
    """Надёжно извлекает финальный JSON-объект из ответа модели (устойчив к `<think>`, markdown и пояснениям)."""
    if isinstance(raw, dict):
        return raw
    orig = (raw or "").strip()
    if not orig:
        raise BrainError("Модель вернула пустой ответ")

    for candidate_text in (strip_thinking_blocks(orig), orig):
        if not candidate_text:
            continue
        try:
            obj = json.loads(candidate_text)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass

        # Проверяем все markdown-блоки с конца к началу
        md_matches = list(JSON_BLOCK_RE.finditer(candidate_text))
        for m in reversed(md_matches):
            try:
                obj = json.loads(m.group(1))
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                continue

        # Сканируем сбалансированные JSON-объекты справа налево через `raw_decode`
        decoder = json.JSONDecoder()
        found_dicts: list[dict] = []
        pos = len(candidate_text)
        while True:
            brace_idx = candidate_text.rfind("{", 0, pos)
            if brace_idx == -1:
                break
            pos = brace_idx
            snippet = candidate_text[brace_idx:]
            try:
                obj, _ = decoder.raw_decode(snippet)
                if isinstance(obj, dict):
                    # Отдаём приоритет целевому объекту с ключом `body` / `score` / `product`
                    if any(k in obj for k in ("body", "score", "product", "icp")):
                        return obj
                    found_dicts.append(obj)
            except ValueError:
                continue

        if found_dicts:
            return found_dicts[0]

    raise BrainError(f"Ответ модели не содержит корректного JSON-объекта: {orig[:220]}")


def mask_api_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 10:
        return k[:2] + "•••"
    return f"{k[:6]}•••{k[-4:]}"


def _auto_select_api_target(cfg: FridayConfig, env: EnvConfig, explicit_key: str) -> tuple[str, str, str]:
    """Сам выбирает оптимальный URL и модель с учётом переменных окружения и формата API-ключа."""
    key = (explicit_key or env.effective_llm_key()).strip()
    cfg_model = (cfg.brain.model or "").strip()
    env_model = env.effective_model()
    chosen_model = cfg_model or env_model

    cfg_base = (cfg.brain.api_base_url or "").strip()
    env_base = env.effective_base_url()
    has_custom_cfg_base = bool(cfg_base and cfg_base.rstrip("/") != DEFAULT_OPENROUTER_BASE)
    custom_base = env_base or (cfg_base if has_custom_cfg_base else "")

    if not key:
        return (
            custom_base or cfg_base or DEFAULT_OPENROUTER_BASE,
            chosen_model or "anthropic/claude-3.5-sonnet",
            "",
        )

    # 1. Нативный ключ Anthropic (`sk-ant-...` или ANTHROPIC_API_KEY)
    if key.startswith("sk-ant-") or (env.anthropic_api_key and key == env.anthropic_api_key and not custom_base):
        base = env_base or "https://api.anthropic.com/v1"
        model = chosen_model if (chosen_model and "claude" in chosen_model.lower()) else "claude-3-5-sonnet-latest"
        return (base, model, key)

    # 2. Ключ OpenRouter (`sk-or-...` или OPENROUTER_API_KEY)
    if key.startswith("sk-or-") or (env.openrouter_api_key and key == env.openrouter_api_key and not custom_base):
        base = custom_base or DEFAULT_OPENROUTER_BASE
        model = chosen_model or "anthropic/claude-3.5-sonnet"
        return (base, model, key)

    # 3. Если пользователем явно задан свой шлюз (ProxyAPI, VseGPT, Polza, LiteLLM, vLLM и т.д.)
    if custom_base:
        default_m = "anthropic/claude-3.5-sonnet" if "openrouter" in custom_base.lower() else "gpt-4o"
        return (custom_base, chosen_model or default_m, key)

    # 4. Ключ DeepSeek
    if env.deepseek_api_key and key == env.deepseek_api_key:
        return ("https://api.deepseek.com/v1", chosen_model or "deepseek-chat", key)

    # 5. Стандартный ключ OpenAI (`sk-...` / `sk-proj-...`)
    if key.startswith("sk-") or (env.openai_api_key and key == env.openai_api_key):
        model = chosen_model if (chosen_model and "/" not in chosen_model) else "gpt-4o"
        return ("https://api.openai.com/v1", model, key)

    return (
        cfg_base or DEFAULT_OPENROUTER_BASE,
        chosen_model or "anthropic/claude-3.5-sonnet",
        key,
    )


async def _probe_local_ollama() -> str:
    """Проверяет, запущена ли локально Ollama (`127.0.0.1:11434`)."""
    try:
        async with httpx.AsyncClient(timeout=0.6) as client:
            r = await client.get("http://127.0.0.1:11434/api/tags")
        if r.status_code != 200:
            return ""
        models = [str(m.get("name") or "") for m in (r.json().get("models") or []) if m.get("name")]
        if not models:
            return ""
        for pref in ("qwen2.5", "qwen3", "llama3", "mistral", "gemma"):
            for m in models:
                if pref in m.lower():
                    return m
        return models[0]
    except Exception:
        return ""


def optimal_temperature(cfg: FridayConfig, task: str = "") -> float:
    if task == "humanize":
        return 0.28
    if cfg.compliance.regulated_niche != "none":
        return 0.46
    return 0.56


def _split_prompt_messages(prompt: str) -> list[dict[str, str]]:
    """Разделяет объединённый промпт на `system` и `user` сообщения для корректной работы строгих LLM API."""
    raw = (prompt or "").strip()
    if "\n\n---\n\n" in raw:
        sys_part, user_part = raw.split("\n\n---\n\n", 1)
        if sys_part.strip() and user_part.strip():
            return [
                {"role": "system", "content": sys_part.strip()},
                {"role": "user", "content": user_part.strip()},
            ]
    return [{"role": "user", "content": raw}]


class Brain:
    """Самонастраивающийся Мозг F.R.I.D.A.Y. (`AutoBrain`)."""

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
        if self.state is not None:
            stored = (await self.state.get_setting(SETTING_LLM_API_KEY)).strip()
            if stored:
                return stored
        return load_env().effective_llm_key()

    async def resolve_optimal_target(self) -> tuple[str, str, str]:
        stored_or_override = ""
        if self._api_key_override:
            stored_or_override = self._api_key_override
        elif self.state is not None:
            stored_or_override = (await self.state.get_setting(SETTING_LLM_API_KEY)).strip()
        return _auto_select_api_target(self.config, load_env(), stored_or_override)

    async def resolve_api_target(self) -> tuple[str, str, str]:
        """Совместимый алиас `resolve_optimal_target` для веб-панели."""
        return await self.resolve_optimal_target()

    async def inspect_status(self) -> dict:
        cfg_b = self.config.brain
        base_url, best_model, api_key = await self.resolve_optimal_target()
        has_api = bool(api_key)

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

        ollama_model = ""
        if not has_api and not claude_ok and not codex_ok:
            ollama_model = await _probe_local_ollama()

        selected = cfg_b.provider
        if selected == "auto":
            if has_api:
                effective = "api"
            elif claude_ok:
                effective = "claude"
                best_model = cfg_b.model or "sonnet"
            elif codex_ok:
                effective = "codex"
                best_model = cfg_b.model or "auto"
            elif ollama_model:
                effective = "ollama"
                base_url = "http://127.0.0.1:11434/v1"
                best_model = ollama_model
            else:
                effective = "agent"
        else:
            effective = selected

        ready_direct = (
            (effective == "api" and has_api)
            or (effective == "claude" and claude_ok)
            or (effective == "codex" and codex_ok)
            or (effective == "ollama" and bool(ollama_model))
        )

        return {
            "configured_provider": selected,
            "effective_provider": effective,
            "effective_label": PROVIDER_LABELS.get(effective, effective),
            "direct_inference_ready": ready_direct,
            "api_base_url": base_url,
            "model": best_model,
            "reasoning_effort": "high",
            "has_api_key": has_api,
            "masked_api_key": mask_api_key(api_key),
            "humanizer": True,
            "runtimes": {
                "api": {"available": has_api},
                "claude": {"available": claude_ok, "reason": claude_reason},
                "codex": {"available": codex_ok, "reason": codex_reason},
                "ollama": {"available": bool(ollama_model), "model": ollama_model},
                "agent": {"available": True},
            },
        }

    async def _call_http_llm(
        self,
        prompt: str,
        *,
        timeout: float = 90.0,
        task: str = "",
        base_override: str = "",
        model_override: str = "",
    ) -> str:
        auto_base, auto_model, api_key = await self.resolve_optimal_target()
        base = (base_override or auto_base).strip().rstrip("/")
        model = (model_override or auto_model).strip()
        temp = optimal_temperature(self.config, task=task)
        is_local = "://localhost" in base.lower() or "://127.0.0.1" in base.lower()

        if not api_key and not is_local:
            raise BrainUnavailableError("API-ключ LLM не обнаружен в окружении.")

        msgs = _split_prompt_messages(prompt)
        sys_msg = next((m["content"] for m in msgs if m["role"] == "system"), "")
        user_msgs = [m for m in msgs if m["role"] != "system"] or [{"role": "user", "content": prompt}]

        # 1. Нативный Anthropic Messages API
        if "api.anthropic.com" in base.lower():
            url = base if base.endswith("/messages") else f"{base}/messages"
            if "/v1/" not in url and not url.endswith("/v1/messages"):
                url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            model_name = model.split("/", 1)[-1] if "/" in model else (model or "claude-3-5-sonnet-latest")
            payload: dict = {
                "model": model_name,
                "max_tokens": 3500,
                "temperature": temp,
                "messages": user_msgs,
            }
            if sys_msg:
                payload["system"] = sys_msg
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise BrainError(f"Ошибка Anthropic API ({resp.status_code}): {resp.text[:300]}")
            txt = extract_llm_text_from_payload(resp.json())
            if not txt:
                raise BrainError("Anthropic API вернул пустой текстовый блок.")
            return txt

        # 2. OpenAI-совместимый API (OpenAI, OpenRouter, DeepSeek, ProxyAPI, VseGPT, Ollama и др.)
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        is_openrouter = "openrouter.ai" in url.lower()
        if is_openrouter:
            headers["HTTP-Referer"] = "https://github.com/Everick-Ven/F.R.I.D.A.Y."
            headers["X-Title"] = "F.R.I.D.A.Y."

        primary_model = model or ("anthropic/claude-3.5-sonnet" if is_openrouter else "gpt-4o")
        candidate_models = [primary_model]
        if is_openrouter:
            for fb_m in OPENROUTER_FALLBACK_MODELS:
                if fb_m not in candidate_models:
                    candidate_models.append(fb_m)

        last_err = ""
        async with httpx.AsyncClient(timeout=timeout) as client:
            for cand_model in candidate_models:
                is_reasoning = bool(OPENAI_REASONING_MODEL_RE.search(cand_model)) or "reasoner" in cand_model.lower()
                if is_reasoning and not is_openrouter:
                    req_body: dict = {
                        "model": cand_model,
                        "max_completion_tokens": 4096,
                        "messages": msgs,
                    }
                else:
                    req_body = {
                        "model": cand_model,
                        "max_tokens": 3500,
                        "temperature": temp,
                        "messages": msgs,
                    }

                resp = await client.post(url, headers=headers, json=req_body)

                # Если шлюз не принял `max_tokens` / `temperature` (например, новая reasoning-модель) — автоповтор без лишних полей
                if resp.status_code == 400 and ("temperature" in resp.text or "max_tokens" in resp.text):
                    minimal_body = {"model": cand_model, "messages": [{"role": "user", "content": prompt}]}
                    resp = await client.post(url, headers=headers, json=minimal_body)

                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:260]}"
                    continue

                data = resp.json()
                txt = extract_llm_text_from_payload(data)
                if txt:
                    return txt

                # Если провайдер вернул пустой `content` при раздельном `system`+`user` — пробуем единым сообщением
                retry_resp = await client.post(
                    url,
                    headers=headers,
                    json={"model": cand_model, "messages": [{"role": "user", "content": prompt}]},
                )
                if retry_resp.status_code < 400:
                    txt2 = extract_llm_text_from_payload(retry_resp.json())
                    if txt2:
                        return txt2

                last_err = f"пустой ответ от модели {cand_model}"

        raise BrainError(f"Не удалось получить ответ от LLM API ({last_err or 'пустой ответ'})")

    async def think(
        self,
        prompt: str,
        *,
        timeout: float = 120.0,
        task: str = "",
    ) -> tuple[str, str]:
        """Выполняет вызов оптимальной модели с защитой от пустого ответа."""
        info = await self.inspect_status()
        eff = info["effective_provider"]

        if eff == "api":
            text = await self._call_http_llm(
                prompt,
                timeout=timeout,
                task=task,
                base_override=info["api_base_url"],
                model_override=info["model"],
            )
            return text, f"api:{info['model']}"
        if eff == "ollama":
            text = await self._call_http_llm(
                prompt,
                timeout=timeout,
                task=task,
                base_override="http://127.0.0.1:11434/v1",
                model_override=info["model"],
            )
            return text, f"ollama:{info['model']}"
        if eff == "claude":
            if not info["runtimes"]["claude"]["available"]:
                raise BrainUnavailableError(info["runtimes"]["claude"]["reason"])
            text = await asyncio.to_thread(
                run_claude_isolated,
                prompt,
                model=info["model"] if info["model"] != "anthropic/claude-3.5-sonnet" else "sonnet",
                timeout=timeout,
            )
            if not text.strip():
                raise BrainError("Claude CLI вернул пустой ответ.")
            return text, "claude"
        if eff == "codex":
            if not info["runtimes"]["codex"]["available"]:
                raise BrainUnavailableError(info["runtimes"]["codex"]["reason"])
            text = await asyncio.to_thread(
                run_codex_isolated,
                prompt,
                model="" if info["model"] in ("auto", "anthropic/claude-3.5-sonnet") else info["model"],
                reasoning_effort="high",
                timeout=timeout,
            )
            if not text.strip():
                raise BrainError("Codex CLI вернул пустой ответ.")
            return text, "codex"

        raise BrainUnavailableError("Прямой рантайм LLM недоступен; активен ИИ-агент рабочей среды.")

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
        raw_sig = str(parsed.get("signal_used") or "").strip()
        from friday.signals import signal_label_ru
        signal_used = signal_label_ru(raw_sig) if raw_sig.isupper() and "_" in raw_sig else raw_sig
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
