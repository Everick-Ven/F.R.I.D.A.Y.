"""Детерминированный скоринг соответствия целевой нише РФ (0..100 баллов, без LLM).

Оценивает любую российскую компанию и ЛПР на основе:
- совпадения географии, отрасли и роли ЛПР с настройками `friday.local.yaml` (`icp`);
- подтверждённых наблюдений из `observations` (найм на hh.ru, связка реклама + дыры конверсии,
  позиции 11–30 в Яндексе, карточки 2ГИС / Яндекс Карт, техническое состояние сайта);
- наличия прямого публичного канала связи с ЛПР (Telegram, личный e-mail, WhatsApp, TenChat).
"""

from __future__ import annotations

from friday.config import FridayConfig
from friday.models.company import Company
from friday.models.prospect import Prospect

SIGNAL_WEIGHTS: dict[str, int] = {
    "HH_HIRING_ROLE": 18,
    "HH_OPEN_ROLES": 8,
    "YANDEX_PAGE2_RANK": 16,
    "YANDEX_DIRECT_ACTIVE": 14,
    "VK_PIXEL_ACTIVE": 8,
    "NO_ONLINE_CAPTURE": 14,
    "NO_YANDEX_METRIKA": 12,
    "NO_MESSENGER_WIDGET": 10,
    "NO_MOBILE_VIEWPORT": 12,
    "THIN_SERVICE_STRUCTURE": 10,
    "LOW_MAP_RATING": 12,
    "NO_WEBSITE_ON_MAP": 10,
    "SLOW_SITE": 8,
    "SSL_ISSUE": 10,
    "NO_HTTPS_REDIRECT": 6,
    "OUTDATED_COPYRIGHT": 6,
    "TELEGRAM_PUBLIC": 8,
    "WHATSAPP_PUBLIC": 5,
    "DECISION_MAKER_FOUND": 10,
    "PERSONAL_EMAIL_FOUND": 8,
    "SITE_UNREACHABLE": -20,
}

EXECUTIVE_ROLES = {
    "icp_decision_maker",
    "ceo",
    "commercial_director",
    "coo",
    "bizdev_director",
    "cmo",
    "procurement_director",
    "logistics_director",
    "cto",
    "cfo",
    "head_of_sales",
    "head_of_marketing",
    "head_of_procurement",
    "managing_partner",
    "general_manager",
    "founder",
    "owner",
    "chief_medical_officer",
    "chief_engineer",
    "director",
    "sole_proprietor",
}


def compute_fit_score(
    company: Company | None,
    prospect: Prospect,
    observations: list[dict],
    config: FridayConfig,
) -> tuple[int, list[str]]:
    score = 22
    reasons: list[str] = []

    target_cities = {c.lower() for c in config.icp.cities}
    city = (prospect.city or (company.city if company else "")).strip()
    if city and (not target_cities or city.lower() in target_cities):
        score += 10
        reasons.append(f"Целевой город: {city}")

    # Совпадение по отрасли / поисковым запросам ICP
    comp_ind = ((company.industry if company else "") or prospect.industry).lower()
    comp_desc = (company.description if company else "").lower()
    icp_terms = [x.lower() for x in [*config.icp.industries, *config.icp.search_queries] if x.strip()]
    for term in icp_terms:
        words = [w for w in term.split() if len(w) >= 4]
        if term in comp_ind or (words and any(w in comp_ind or w in comp_desc for w in words)):
            score += 8
            reasons.append(f"Совпадение с нишей ICP ({company.industry if company and company.industry else term})")
            break

    # Уровень ЛПР
    custom_titles = [t.lower() for t in config.icp.decision_maker_titles if t.strip()]
    title_l = (prospect.title or "").lower()
    if custom_titles and any(ct in title_l for ct in custom_titles):
        score += 14
        reasons.append(f"Целевая должность ЛПР: {prospect.title}")
    elif prospect.role_kind in EXECUTIVE_ROLES:
        score += 10
        reasons.append(f"Руководящая роль: {prospect.title}")

    # Прямые каналы связи
    if prospect.telegram or (company and company.telegram):
        score += 8
        reasons.append("Доступен публичный Telegram")
    if prospect.email and prospect.email_source == "personal_page":
        score += 8
        reasons.append("Найден прямой рабочий e-mail")
    elif prospect.email:
        score += 4
    if prospect.tenchat or (company and company.tenchat):
        score += 4

    seen_codes: set[str] = set()
    for obs in observations:
        code = obs.get("signal_code", "")
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        delta = SIGNAL_WEIGHTS.get(code, 0)
        if delta:
            score += delta
            detail = obs.get("value_text") or code
            reasons.append(f"{code}: {detail}")

    # Синергетические связки сигналов (сильнейшие поводы для касания в любой нише)
    if ("YANDEX_DIRECT_ACTIVE" in seen_codes or "VK_PIXEL_ACTIVE" in seen_codes) and (
        "NO_ONLINE_CAPTURE" in seen_codes
        or "NO_MESSENGER_WIDGET" in seen_codes
        or "NO_YANDEX_METRIKA" in seen_codes
        or "NO_MOBILE_VIEWPORT" in seen_codes
    ):
        score += 12
        reasons.append("Платный трафик идёт на посадочную с потерей конверсии")

    if "HH_HIRING_ROLE" in seen_codes and (
        "NO_ONLINE_CAPTURE" in seen_codes or "CRM_WIDGET_ACTIVE" not in seen_codes
    ):
        score += 8
        reasons.append("Активный найм на hh.ru при ручной обработке заявок на сайте")

    final_score = max(0, min(100, score))
    return final_score, reasons
