"""Модели данных F.R.I.D.A.Y."""

from friday.models.company import Company
from friday.models.draft import Draft
from friday.models.prospect import COMPANY_CONTACT_SOURCE, Prospect

__all__ = ["Company", "Prospect", "Draft", "COMPANY_CONTACT_SOURCE"]
