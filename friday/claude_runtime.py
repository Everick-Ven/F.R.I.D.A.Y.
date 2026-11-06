"""Изолированный запуск локальных CLI `claude` и `codex` без инструментов и без утечки контекста (по схеме Harvey).

Промпты F.R.I.D.A.Y. содержат недоверенный внешний текст с сайтов (`<untrusted>`), а результат
идёт внешним получателям, поэтому каждый вызов CLI обязан видеть ТОЛЬКО переданный промпт:
- пустая временная директория (`cwd`), без чтения `CLAUDE.md`, `AGENTS.md`, плагинов, хуков и MCP;
- промпт передаётся строго через `stdin` (никогда через аргументы командной строки `argv`);
- отключены все встроенные инструменты (`--tools ""`) и сохранение сессий.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REQUIRED_CLAUDE_FLAGS = (
    "--safe-mode",
    "--setting-sources",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--tools",
    "--output-format",
)

STRIPPED_ENV = (
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    "CLAUDE_CODE_SIMPLE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_ENV_FILE",
    "CLAUDE_PROJECT_DIR",
    "CLAUDECODE",
    "OLDPWD",
)


class CliRuntimeError(RuntimeError):
    """Локальный CLI недоступен или не поддерживает обязательные флаги изоляции."""


def _clean_env(tmpdir: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    env["PWD"] = tmpdir
    return env


def check_claude_cli() -> str:
    """Возвращает путь к изолированному `claude` CLI либо выбрасывает `CliRuntimeError`."""
    binary = shutil.which("claude")
    if not binary:
        raise CliRuntimeError("Claude Code CLI (`claude`) не установлен в системе.")
    try:
        proc = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        raise CliRuntimeError(f"Не удалось проверить `claude --help`: {exc}") from exc

    help_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    missing = [flag for flag in REQUIRED_CLAUDE_FLAGS if flag not in help_text]
    if missing:
        raise CliRuntimeError(
            f"Установленный `claude` CLI не поддерживает флаги изоляции Harvey: {', '.join(missing)}"
        )
    return binary


def run_claude_isolated(
    prompt: str,
    *,
    model: str = "",
    timeout: float = 180.0,
) -> str:
    """Выполняет изолированный вызов `claude -p` без инструментов, плагинов и истории."""
    binary = check_claude_cli()
    cmd = [
        binary,
        "-p",
        "--safe-mode",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--tools",
        "",
        "--output-format",
        "text",
    ]
    clean_model = (model or "").strip()
    if clean_model and "/" not in clean_model:
        cmd.extend(["--model", clean_model])

    with tempfile.TemporaryDirectory(prefix="friday-claude-") as tmpdir:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=tmpdir,
            env=_clean_env(tmpdir),
            timeout=timeout,
            check=False,
        )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise CliRuntimeError(f"Ошибка вызова `claude` CLI (код {proc.returncode}): {err}")
    return (proc.stdout or "").strip()


def check_codex_cli() -> str:
    """Возвращает путь к `codex` CLI либо выбрасывает `CliRuntimeError`."""
    binary = shutil.which("codex")
    if not binary:
        raise CliRuntimeError("Codex CLI (`codex`) не установлен в системе.")
    return binary


def run_codex_isolated(
    prompt: str,
    *,
    model: str = "",
    reasoning_effort: str = "medium",
    timeout: float = 180.0,
) -> str:
    """Выполняет изолированный вызов `codex exec` только на чтение в пустой директории."""
    binary = check_codex_cli()
    with tempfile.TemporaryDirectory(prefix="friday-codex-") as tmpdir:
        out_path = Path(tmpdir) / "last_message.txt"
        cmd = [
            binary,
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-last-message",
            str(out_path),
        ]
        clean_model = (model or "").strip()
        if clean_model and "/" not in clean_model:
            cmd.extend(["--model", clean_model])
        if reasoning_effort in ("low", "medium", "high"):
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        cmd.append("-")

        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=tmpdir,
            env=_clean_env(tmpdir),
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:400]
            raise CliRuntimeError(f"Ошибка вызова `codex` CLI (код {proc.returncode}): {err}")
        if out_path.exists():
            return out_path.read_text(encoding="utf-8").strip()
        return (proc.stdout or "").strip()
