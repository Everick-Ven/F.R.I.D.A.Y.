"""Модель целевой компании на российском рынке (универсальная под любую нишу)."""

from dataclasses import dataclass, field


@dataclass
class Company:
    id: str = ""
    name: str = ""
    domain: str = ""
    website: str = ""
    industry: str = ""            # Отрасль / ниша (стоматология, логистика, SaaS, производство и т.д.)
    subcategories: list[str] = field(default_factory=list)
    company_size: str = ""
    inn: str = ""                 # ИНН компании (при импорте из баз контрагентов или футера сайта)
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
    hh_url: str = ""              # Ссылка на страницу работодателя на hh.ru
    external_id: str = ""         # Стабильный ключ: "2gis:...", "ymaps:...", "hh:...", "inn:...", "osm:..."
    source: str = ""
    source_url: str = ""
    tech_stack: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    notes: str = ""
    profile_checked_at: str | None = None
    prospects_checked_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
