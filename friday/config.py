"""Загрузка и валидация конфигурации F.R.I.D.A.Y. (friday.yaml / friday.local.yaml + .env)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from friday.paths import PROJECT_ROOT
from friday.textio import read_user_text


class ConfigError(ValueError):
    """Ошибка в структуре конфигурации."""


class ConfigFileNotFoundError(FileNotFoundError):
    """Файл friday.yaml не найден."""


BrainProvider = Literal["claude", "agent", "openai_compatible", "template"]


class PersonaConfig(BaseModel):
    name: str = "Ваше имя"
    company: str = "Бюро юридического трафика"
    role: str = "Руководитель бюро"
    telegram: str = ""
    website: str = ""
    tone: str = "спокойный, уважительный, коллегиальный, без рекламного нажима"
    address_style: Literal["formal_vy", "polite_name"] = "formal_vy"


class BureauConfig(BaseModel):
    name: str = "Привлечение доверителей для адвокатов и юридических практик"
    description: str = (
        "Настраиваем системное привлечение доверителей для адвокатов, бюро и коллегий "
        "в России по правилам ФЗ «О рекламе» и ст. 17 КПЭА."
    )
    principles: list[str] = Field(default_factory=lambda: [
        "Строгое соблюдение 38-ФЗ «О рекламе» и ст. 17 КПЭА",
        "Не даём пустых гарантий потока заявок или исхода дел",
        "Все рекламные кабинеты, домены и аналитика остаются на стороне доверителя",
    ])
    entry_offer: str = (
        "Короткий текстовый разбор точек потери обращений в Яндексе и картах (5–7 пунктов)"
    )
    primary_channels: list[str] = Field(default_factory=lambda: ["telegram", "email"])


class BrainConfig(BaseModel):
    provider: BrainProvider = "claude"
    model: str = ""

    @field_validator("model")
    @classmethod
    def _clean_model(cls, v: str) -> str:
        val = (v or "").strip()
        if val and any(ch.isspace() for ch in val):
            raise ValueError("Имя модели не должно содержать пробелов")
        return val


class ICPConfig(BaseModel):
    legal_forms: list[str] = Field(default_factory=lambda: [
        "Коллегия адвокатов",
        "Адвокатское бюро",
        "Адвокатский кабинет",
    ])
    practice_areas: list[str] = Field(default_factory=lambda: [
        "адвокат по уголовным делам",
        "адвокат по семейным спорам",
        "адвокат по арбитражным спорам",
        "адвокат по банкротству",
    ])
    cities: list[str] = Field(default_factory=lambda: ["Москва", "Санкт-Петербург"])
    decision_maker_titles: list[str] = Field(default_factory=lambda: [
        "Управляющий партнёр",
        "Председатель коллегии",
        "Старший партнёр",
        "Партнёр",
        "Руководитель практики",
        "Адвокат",
    ])
    team_size_hint: str = "от 1 до 50 адвокатов/юристов"


class ComplianceConfig(BaseModel):
    mode: Literal["recon_and_drafts_only"] = "recon_and_drafts_only"
    enforce_fz38: bool = True
    enforce_kpea: bool = True
    max_telegram_words: int = 85
    max_email_words: int = 140
    do_not_contact: list[str] = Field(default_factory=list)

    @field_validator("do_not_contact", mode="before")
    @classmethod
    def _normalize_dnc(cls, v) -> list[str]:
        if not v:
            return []
        from friday.gate import normalize_dnc_list
        return normalize_dnc_list(v)


class UsageConfig(BaseModel):
    max_daily_brain_calls: int = 100
    discover_max_spend_rub: float = 300.0


class FridayConfig(BaseModel):
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    bureau: BureauConfig = Field(default_factory=BureauConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    icp: ICPConfig = Field(default_factory=ICPConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)
    source_path: str = Field(default="", exclude=True)


class EnvConfig(BaseModel):
    yandex_search_api_key: str = ""
    yandex_search_folder_id: str = ""
    yandex_xml_url: str = ""
    yandex_maps_api_key: str = ""
    twogis_api_key: str = ""
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""

    def as_dict(self) -> dict[str, str]:
        return self.model_dump()


def find_config_file(explicit_path: str | Path | None = None) -> Path:
    """Найти активный файл конфигурации (`friday.local.yaml` имеет приоритет над `friday.yaml`)."""
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            raise ConfigFileNotFoundError(f"Файл конфигурации не найден: {p}")
        return p

    candidates = [
        Path.cwd() / "friday.local.yaml",
        PROJECT_ROOT / "friday.local.yaml",
        Path.cwd() / "friday.yaml",
        PROJECT_ROOT / "friday.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise ConfigFileNotFoundError(
        "Не найден friday.yaml (или friday.local.yaml). Проверьте корень репозитория."
    )


def load_config(path: str | Path | None = None) -> FridayConfig:
    """Прочитать и провалидировать конфигурацию F.R.I.D.A.Y."""
    cfg_path = find_config_file(path)
    try:
        raw = yaml.safe_load(read_user_text(cfg_path)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ошибка синтаксиса YAML в {cfg_path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Корневой элемент {cfg_path.name} должен быть словарём YAML.")
    try:
        cfg = FridayConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Некорректные параметры в {cfg_path.name}: {exc}") from exc
    cfg.source_path = str(cfg_path.resolve())
    return cfg


def load_env(env_path: str | Path | None = None) -> EnvConfig:
    """Прочитать переменные окружения из `.env` и окружения процесса."""
    target = Path(env_path) if env_path else (PROJECT_ROOT / ".env")
    if target.exists():
        load_dotenv(dotenv_path=str(target), override=False)
    return EnvConfig(
        yandex_search_api_key=os.getenv("YANDEX_SEARCH_API_KEY", "").strip(),
        yandex_search_folder_id=os.getenv("YANDEX_SEARCH_FOLDER_ID", "").strip(),
        yandex_xml_url=os.getenv("YANDEX_XML_URL", "").strip(),
        yandex_maps_api_key=os.getenv("YANDEX_MAPS_API_KEY", "").strip(),
        twogis_api_key=os.getenv("TWOGIS_API_KEY", "").strip(),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "").strip(),
    )
