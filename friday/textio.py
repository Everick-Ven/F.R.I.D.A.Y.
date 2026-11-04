"""Надёжное чтение текстовых файлов и настройка UTF-8 вывода (включая поддержку Windows-1251 для РФ)."""

import locale
import logging
import sys
from pathlib import Path

logger = logging.getLogger("friday.textio")


def _newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_russian_bytes(data: bytes, source_hint: str = "") -> str:
    """Декодировать байты в строку: UTF-8 (с BOM или без) -> CP1251 -> системная кодировка."""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            if enc != "utf-8-sig" and source_hint:
                logger.info("Файл %s прочитан в кодировке %s (рекомендуется сохранить в UTF-8).", source_hint, enc)
            return _newlines(text)
        except UnicodeDecodeError:
            continue
    fallback = locale.getencoding() or "utf-8"
    return _newlines(data.decode(fallback, errors="replace"))


def read_user_text(path: str | Path) -> str:
    """Прочитать пользовательский файл (friday.yaml, .env, CSV, промпт)."""
    p = Path(path)
    return decode_russian_bytes(p.read_bytes(), source_hint=str(p))


def utf8_stdio(*streams) -> None:
    """Гарантировать UTF-8 в stdout/stderr на всех ОС."""
    for stream in streams or (sys.stdout, sys.stderr):
        errors = "backslashreplace" if stream is sys.stderr else "replace"
        try:
            stream.reconfigure(encoding="utf-8", errors=errors)
        except (AttributeError, ValueError, OSError):
            pass
