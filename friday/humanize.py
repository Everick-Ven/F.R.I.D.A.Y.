"""Второй проход Humanizer (по схеме Harvey): убирает нейросетевые обороты из готового черновика.

Включается флагом `writer.humanizer: true` в конфигурации (или в настройках Мозга в UI).
Работает по принципу fail-open с жёстким детерминированным контролем:
- если модель вернула мета-комментарий («Вот исправленный вариант...»), добавила длинное тире (`—`),
  потеряла переменные или раздула объём больше чем на 2 слова — возвращается исходный черновик;
- после Humanizer каждый черновик всё равно проходит обязательный шлюз `friday.gate.validate_draft`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from friday.paths import SKILLS_DIR
from friday.textio import read_user_text
from friday.untrusted import UNTRUSTED_RULE, wrap_untrusted

MERGE_VAR = re.compile(r"\{\{\s*[\wа-яА-ЯёЁ]+\s*\}\}")
MAX_GROWTH_WORDS = 2

META_PREFIXES = (
    "вот ",
    "конечно,",
    "извините",
    "я не могу",
    "как ии",
    "как языковая модель",
    "исправленный",
    "переписанный",
    "i can't",
    "i cannot",
    "as an ai",
    "sorry,",
    "here is",
    "here's",
)


def humanizer_enabled(config) -> bool:
    return bool(getattr(getattr(config, "writer", None), "humanizer", False))


def load_humanizer_skill(path: Path | None = None) -> str:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    env_p = os.getenv("FRIDAY_HUMANIZER_SKILL", "").strip()
    if env_p:
        candidates.append(Path(env_p))
    candidates.append(SKILLS_DIR / "humanizer.md")
    candidates.append(Path.home() / ".claude" / "skills" / "humanizer" / "SKILL.md")

    for candidate in candidates:
        if candidate.exists():
            try:
                text = read_user_text(candidate)
            except OSError:
                continue
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end != -1:
                    text = text[end + 4 :]
            cleaned = text.strip()
            if cleaned:
                return cleaned
    return ""


def build_humanizer_prompt(draft: dict, skill_text: str | None = None) -> str:
    skill = skill_text if skill_text is not None else load_humanizer_skill()
    draft_json = json.dumps(
        {"subject": draft.get("subject", ""), "body": draft.get("body", "")},
        ensure_ascii=False,
    )
    return (
        "Ты — редактор живой деловой переписки (проход Humanizer). "
        "Перепиши черновик так, чтобы он звучал как короткое сообщение живого человека, "
        "написанное руками за минуту, без нейросетевого лоска и канцелярита.\n\n"
        f"{skill}\n\n"
        "ЖЁСТКИЕ ОГРАНИЧЕНИЯ РЕДАКТУРЫ:\n"
        "- Не добавляй ни одного нового факта о получателе или отправителе.\n"
        "- Не меняй обращение по имени-отчеству и название организации.\n"
        "- Никаких длинных тире (—), восклицательных знаков, списков и форматирования.\n"
        "- Текст НЕ должен стать длиннее оригинала.\n"
        "- Оставь ровно один спокойный вопрос в конце.\n\n"
        f"{wrap_untrusted(draft_json, source='draft_to_humanize')}\n\n"
        f"{UNTRUSTED_RULE}\n\n"
        'Верни ТОЛЬКО валидный JSON вида {"subject": "...", "body": "..."} без пояснений.'
    )


def acceptable_rewrite(original: dict, rewrite: dict | None) -> bool:
    if not isinstance(rewrite, dict):
        return False
    body = str(rewrite.get("body") or "").strip()
    subject = str(rewrite.get("subject") or "").strip()
    if not body:
        return False
    if original.get("subject") and not subject:
        return False
    low_body = body.lower()
    if any(low_body.startswith(p) for p in META_PREFIXES):
        return False
    before = str(original.get("subject", "")) + str(original.get("body", ""))
    after = subject + body
    if sorted(MERGE_VAR.findall(before)) != sorted(MERGE_VAR.findall(after)):
        return False
    if "—" in after and "—" not in before:
        return False
    if "!" in after and "!" not in before:
        return False
    return len(body.split()) <= len(str(original.get("body", "")).split()) + MAX_GROWTH_WORDS


async def humanize_draft(
    brain,
    draft: dict,
    *,
    enabled: bool = False,
    skill_path: Path | None = None,
) -> dict:
    """Прогоняет один черновик через Humanizer; при любом нарушении возвращает исходный."""
    if not enabled or not draft or not draft.get("body"):
        return draft
    skill = load_humanizer_skill(skill_path)
    if not skill:
        return draft
    prompt = build_humanizer_prompt(draft, skill_text=skill)
    try:
        rewritten = await brain.think_json(prompt, task="humanize")
    except Exception:
        return draft
    if not acceptable_rewrite(draft, rewritten):
        return draft
    merged = dict(draft)
    merged["subject"] = str(rewritten.get("subject") or "").strip()
    merged["body"] = str(rewritten.get("body") or "").strip()
    return merged
