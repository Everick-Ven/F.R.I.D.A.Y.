"""Загрузка, валидация и сохранение конфигурации F.R.I.D.A.Y. (`friday.yaml` / `friday.local.yaml`).

Принцип разделения конфигурации:
- отслеживаемый в Git `friday.yaml` остаётся чистым шаблоном («Ваша компания»);
- на Шаге 1 (задание ниши через `friday train`, веб-панель или ИИ-агента) создаётся
  `friday.local.yaml` (в `.gitignore`), который автоматически имеет приоритет.
"""

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
    """Файл конфигурации не найден."""


UNCONFIGURED_COMPANIES = frozenset({"", "ваша компания", "your company"})
UNCONFIGURED_PRODUCTS = frozenset({"", "ваш продукт или услуга", "your product"})

RegulatedNiche = Literal["none", "medicine", "finance", "legal_advocates"]


class PersonaConfig(BaseModel):
    name: str = "Ваше имя"
    company: str = "Ваша компания"
    role: str = "Основатель / Руководитель направления"
    telegram: str = ""
    website: str = ""
    tone: str = "деловой, спокойный, уважительный, без спама и рекламных штампов"
    address_style: Literal["polite_name", "formal_vy"] = "polite_name"


class OfferConfig(BaseModel):
    entry: str = ""
    goal: Literal["get_reply", "book_call", "send_audit"] = "get_reply"


class ProductConfig(BaseModel):
    name: str = "Ваш продукт или услуга"
    category: str = ""
    description: str = "Опишите, что делает ваш продукт/услуга и какую задачу бизнеса он решает"
    pricing: str = ""
    key_benefits: list[str] = Field(default_factory=list)
    objection_responses: dict[str, str] = Field(default_factory=dict)
    offer: OfferConfig = Field(default_factory=OfferConfig)
    primary_channels: list[str] = Field(default_factory=lambda: ["telegram", "email"])


class ICPConfig(BaseModel):
    niche: str = ""
    industries: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=lambda: ["Москва", "Санкт-Петербург"])
    company_size: str = "10–200 сотрудников"
    decision_maker_titles: list[str] = Field(default_factory=lambda: [
        "Собственник",
        "Основатель",
        "Генеральный директор",
        "Коммерческий директор",
        "Директор по маркетингу",
    ])
    hiring_signals: list[str] = Field(default_factory=list)
    min_fit_score: int = 50


class BrainConfig(BaseModel):
    mode: Literal["agent", "template"] = "agent"
    tasks_dir: str = "data/agent_tasks"


class ComplianceConfig(BaseModel):
    mode: Literal["recon_and_drafts_only"] = "recon_and_drafts_only"
    enforce_fz38: bool = True
    regulated_niche: RegulatedNiche = "none"
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
    discover_max_spend_rub: float = 300.0


class FridayConfig(BaseModel):
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    product: ProductConfig = Field(default_factory=ProductConfig)
    icp: ICPConfig = Field(default_factory=ICPConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)
    source_path: str = Field(default="", exclude=True)

    def is_niche_configured(self) -> bool:
        """Проверка Шага 1: задана ли реальная ниша и продукт вместо стартового шаблона."""
        comp_low = (self.persona.company or "").strip().lower()
        prod_low = (self.product.name or "").strip().lower()
        has_targets = bool(self.icp.niche.strip() or self.icp.search_queries or self.icp.industries)
        if not has_targets:
            return False
        if comp_low in UNCONFIGURED_COMPANIES and prod_low in UNCONFIGURED_PRODUCTS:
            return False
        return True


class EnvConfig(BaseModel):
    yandex_search_api_key: str = ""
    yandex_search_folder_id: str = ""
    yandex_xml_url: str = ""
    yandex_maps_api_key: str = ""
    twogis_api_key: str = ""

    def as_dict(self) -> dict[str, str]:
        return self.model_dump()


def is_niche_configured(cfg: FridayConfig) -> bool:
    """Модульная обёртка проверки Шага 1: задана ли реальная ниша и продукт."""
    return cfg.is_niche_configured()


def find_config_file(explicit_path: str | Path | None = None) -> Path:
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
    raise ConfigFileNotFoundError("Не найден friday.yaml (или friday.local.yaml).")


def load_config(path: str | Path | None = None) -> FridayConfig:
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


def save_local_config(cfg: FridayConfig, target_path: str | Path | None = None) -> Path:
    """Сохранить настроенную нишу и продукт в `friday.local.yaml` (не трогая шаблонный `friday.yaml`)."""
    dest = Path(target_path) if target_path else (PROJECT_ROOT / "friday.local.yaml")
    payload = cfg.model_dump(exclude={"source_path"})
    text = (
        "# Активная конфигурация ниши и продукта F.R.I.D.A.Y. (создана автоматически, в .gitignore)\n\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    )
    dest.write_text(text, encoding="utf-8")
    cfg.source_path = str(dest.resolve())
    return dest


def load_env(env_path: str | Path | None = None) -> EnvConfig:
    target = Path(env_path) if env_path else (PROJECT_ROOT / ".env")
    if target.exists():
        load_dotenv(dotenv_path=str(target), override=False)
    return EnvConfig(
        yandex_search_api_key=os.getenv("YANDEX_SEARCH_API_KEY", "").strip(),
        yandex_search_folder_id=os.getenv("YANDEX_SEARCH_FOLDER_ID", "").strip(),
        yandex_xml_url=os.getenv("YANDEX_XML_URL", "").strip(),
        yandex_maps_api_key=os.getenv("YANDEX_MAPS_API_KEY", "").strip(),
        twogis_api_key=os.getenv("TWOGIS_API_KEY", "").strip(),
    )
