"""Модель контактного лица (адвоката, управляющего партнёра, председателя коллегии)."""

from dataclasses import dataclass

COMPANY_CONTACT_SOURCE = "company_contact"


@dataclass
class Prospect:
    id: str = ""
    company_id: str = ""
    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""         # Отчество — критично для делового общения с адвокатами в РФ
    title: str = ""               # Управляющий партнёр, Председатель коллегии, Адвокат и т.д.
    role_kind: str = ""           # partner | chairman | solo_advocate | head_of_practice | marketing | general
    registry_number: str = ""     # Реестровый номер Минюста (например, "77/12345")
    registry_status: str = ""     # действующий | приостановлен | прекращён | ""
    email: str = ""               # Только публично опубликованный рабочий адрес (не guess!)
    email_source: str = ""        # website_mailto | directory | registry | import
    phone: str = ""
    telegram: str = ""            # @username или https://t.me/...
    whatsapp: str = ""
    tenchat: str = ""
    vk: str = ""
    company: str = ""
    city: str = ""
    source: str = ""
    source_url: str = ""
    status: str = "new"           # new | drafted | approved | contacted_manually | skipped | opted_out
    score: int = 0
    personalization_notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def full_name(self) -> str:
        parts = [p.strip() for p in (self.last_name, self.first_name, self.middle_name) if p and p.strip()]
        return " ".join(parts)

    def polite_salutation(self, style: str = "formal_vy") -> str:
        """Формирует естественное обращение для первого контакта."""
        fn = (self.first_name or "").strip()
        mn = (self.middle_name or "").strip()
        if style == "formal_vy" and fn and mn:
            return f"{fn} {mn}"
        if fn:
            return fn
        return ""
