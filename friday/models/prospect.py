"""Модель ЛПР / контактного лица в целевой компании (универсальная под любую нишу в РФ)."""

from dataclasses import dataclass

COMPANY_CONTACT_SOURCE = "company_contact"


@dataclass
class Prospect:
    id: str = ""
    company_id: str = ""
    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    title: str = ""
    role_kind: str = ""
    registry_id: str = ""
    email: str = ""
    email_source: str = ""
    phone: str = ""
    telegram: str = ""
    whatsapp: str = ""
    tenchat: str = ""
    vk: str = ""
    company: str = ""
    industry: str = ""
    city: str = ""
    source: str = ""
    source_url: str = ""
    status: str = "new"
    score: int = 0
    personalization_notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def full_name(self) -> str:
        parts = [p.strip() for p in (self.last_name, self.first_name, self.middle_name) if p and p.strip()]
        return " ".join(parts) or (self.title or "Руководитель")

    @property
    def polite_greeting(self) -> str:
        fn = (self.first_name or "").strip()
        mn = (self.middle_name or "").strip()
        if fn and mn:
            return f"{fn} {mn}"
        if fn:
            return fn
        return "Коллеги"

    def polite_salutation(self, style: str = "polite_name") -> str:
        fn = (self.first_name or "").strip()
        mn = (self.middle_name or "").strip()
        if style == "formal_vy" and fn and mn:
            return f"{fn} {mn}"
        if fn:
            return fn
        return "Коллеги"
