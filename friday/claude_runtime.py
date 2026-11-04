"""Изолированный запуск Claude CLI по подписке (унаследовано и адаптировано из Harvey).

Каждый вызов `claude -p` выполняется:
- без инструментов (`--tools ""`) и без MCP (`--strict-mcp-config`), чтобы внешний текст с сайтов
  не мог спровоцировать вызов локальных утилит;
- без подгрузки пользовательских `CLAUDE.md`, хуков, плагинов и истории сессий
  (`--safe-mode`, `--setting-sources ""`, `--disable-slash-commands`, `--no-session-persistence`);
- из пустой временной директории вне репозитория, с передачей промпта строго через `stdin`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

REQUIRED_FLAGS = (
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

HELP_TIMEOUT_SECONDS = 30
_MIN_NATIVE_BYTES = 4096
_SHIM_TARGET_RE = re.compile(
    r'"(?:%~?dp0%?\\)?((?:[A-Za-z]:\\)?[^"%]*?\bclaude\.exe)"', re.IGNORECASE
)

_capable: dict[tuple, bool] = {}
_capable_lock = threading.Lock()


class ClaudeRuntimeError(Exception):
    """Claude CLI не найден или не поддерживает флаги изоляции."""


def _shim_target(shim: Path) -> Path:
    try:
        m = _SHIM_TARGET_RE.search(shim.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        m = None
    if m:
        target = m.group(1).replace("\\", "/")
        return Path(target) if re.match(r"[A-Za-z]:/", target) else shim.parent / target
    return shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"


def find_claude(env: dict | None = None) -> str | None:
    path = (env if env is not None else os.environ).get("PATH")
    found = shutil.which("claude", path=path)
    if not found or sys.platform != "win32" or not found.lower().endswith((".cmd", ".bat")):
        return found
    native = _shim_target(Path(found))
    try:
        size = native.stat().st_size if native.is_file() else None
    except OSError:
        size = None
    if size is not None and size >= _MIN_NATIVE_BYTES:
        return str(native)
    raise ClaudeRuntimeError(
        f"Найден Windows-шим {found}, но нативный бинарник {native} отсутствует. "
        "Установите актуальный Claude Code CLI."
    )


def runtime_env(cwd: str, base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    for key in STRIPPED_ENV:
        env.pop(key, None)
    env["PWD"] = cwd
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    return env


def build_command(executable: str, model: str = "", session_id: str | None = None) -> list[str]:
    cmd = [
        executable,
        "-p",
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--safe-mode",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    if session_id:
        try:
            uuid.UUID(session_id)
            cmd += ["--session-id", session_id]
        except ValueError:
            pass
    return cmd


@contextmanager
def isolated_cwd():
    with tempfile.TemporaryDirectory(prefix="friday-claude-", ignore_cleanup_errors=True) as tmp:
        yield str(Path(tmp).resolve())


def _fingerprint(executable: str) -> tuple:
    real = os.path.realpath(executable)
    try:
        st = os.stat(real)
        return (real, st.st_mtime_ns, st.st_size)
    except OSError:
        return (real, None, None)


def missing_flags(help_text: str) -> list[str]:
    return [
        f for f in REQUIRED_FLAGS
        if not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", help_text)
    ]


def check_cli(env: dict | None = None) -> str:
    exe = find_claude(env)
    if not exe:
        raise ClaudeRuntimeError(
            "Claude CLI не найден в PATH. Установите Claude Code и выполните `claude login`, "
            "либо переключите `brain.provider` в friday.yaml на `agent`, `openai_compatible` или `template`."
        )
    key = _fingerprint(exe)
    with _capable_lock:
        if _capable.get(key):
            return exe
    try:
        with isolated_cwd() as cwd:
            out = subprocess.run(
                [exe, "--help"],
                cwd=cwd,
                env=runtime_env(cwd),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=HELP_TIMEOUT_SECONDS,
            )
        out.check_returncode()
        help_text = (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClaudeRuntimeError(
            f"Не удалось проверить `{exe} --help` ({type(exc).__name__})."
        ) from exc

    missing = missing_flags(help_text)
    if missing:
        raise ClaudeRuntimeError(
            f"Установленный Claude CLI ({exe}) не поддерживает флаги изоляции: {', '.join(missing)}. "
            "Обновите CLI командой `claude update`."
        )
    with _capable_lock:
        _capable[key] = True
    return exe
