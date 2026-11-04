"""Модель адвокатского образования / юридической практики."""

from dataclasses import dataclass, field


@dataclass
class Company:
    id: str = ""
    name: str = ""
    domain: str = ""
    website: str = ""
    legal_form: str = ""          # Коллегия адвокатов | Адвокатское бюро | Адвокатский кабинет | Юридическая фирма
    practice_areas: list[str] = field(default_factory=list)
    description: str = ""
    city: str = ""
    address: str = ""
    phone: str = ""
    telegram: str = ""
    whatsapp: str = ""
    vk: str = ""
    tenchat: str = ""
    ymaps_url: str = ""
    twogis_url: str = ""
    registry_region: str = ""
    external_id: str = ""         # Стабильный ключ провайдера: "2gis:...", "ymaps:...", "minjust:77/..."
    source: str = ""
    source_url: str = ""
    tech_stack: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    notes: str = ""
    profile_checked_at: str | None = None
    prospects_checked_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
