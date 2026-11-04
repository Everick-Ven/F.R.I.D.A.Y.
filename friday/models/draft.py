"""Модель персонального черновика и аналитической карточки (только для ручной отправки)."""

from dataclasses import dataclass, field


@dataclass
class Draft:
    id: str = ""
    prospect_id: str = ""
    company_id: str = ""
    channel: str = "telegram"     # telegram | email | tenchat
    subject: str = ""             # Заполняется для email; пусто для Telegram/TenChat
    body: str = ""
    audit_summary: str = ""       # Краткое резюме-досье (3–5 пунктов для самого специалиста перед ручным заходом)
    signal_used: str = ""         # Код главного сигнала, на котором построен заход
    status: str = "pending_review"  # pending_review | approved | sent_manually | rejected | gate_blocked
    gate_notes: list[str] = field(default_factory=list)
    brain_provider: str = ""
    created_at: str = ""
    updated_at: str = ""
