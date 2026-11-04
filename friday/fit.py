"""Расчёт оценки соответствия (Fit Score 0–100) для адвокатского образования / практики.

Считается на лету по данным в БД (форма образования, город, найденный ЛПР, каналы для ручной связи
и подтверждённые сигналы точек роста в Яндексе / 2ГИС / на сайте), взвешенным относительно `friday.yaml`.
"""

from __future__ import annotations

from friday.config import FridayConfig
from friday.gate import match_do_not_contact

# Веса компонентов оценки (в сумме до 100)
WEIGHT_PROFILE = 25          # Форма адвокатского образования и целевой город
WEIGHT_DECISION_MAKER = 25   # Именной ЛПР (управляющий партнёр, председатель, адвокат)
WEIGHT_REACHABLE = 20        # Публичный канал для деликатного ручного контакта (Telegram / Email / форма)
WEIGHT_GROWTH_SIGNAL = 30    # Сигналы точек роста (позиции 11-30 Яндекса, Директ, карты, посадочные, Метрика)


def pick_primary_signal(observations: list[dict]) -> tuple[str, str]:
    """Выбрать один главный сигнал для первого касания по иерархии приоритетов.

    Возвращает `(signal_code, human_description)`.
    """
    by_code: dict[str, dict] = {}
    for obs in observations:
        code = obs.get("signal_code") or ""
        if code and code not in by_code:
            by_code[code] = obs

    # 1. Позиция 11–30 в поиске Яндекса
    if "YANDEX_SERP_RANK" in by_code:
        obs = by_code["YANDEX_SERP_RANK"]
        rank = int(obs.get("value_num") or 0)
        kw = obs.get("value_text") or "профильному запросу"
        if 11 <= rank <= 35:
            return (
                "YANDEX_SERP_RANK",
                f"по запросу «{kw}» сайт находится на {rank}-й позиции в Яндексе (рядом с первой страницей)",
            )
        if rank > 35:
            return (
                "YANDEX_SERP_RANK",
                f"по запросу «{kw}» сайт пока за пределами топ-30 Яндекса ({rank}-я позиция)",
            )

    # 2. Уже крутят рекламу / коллтрекинг + есть потери на сайте
    if "RUNNING_YANDEX_DIRECT" in by_code:
        tools = by_code["RUNNING_YANDEX_DIRECT"].get("value_text") or "Яндекс.Директ"
        if "MISSING_PRACTICE_PAGES" in by_code:
            return (
                "RUNNING_YANDEX_DIRECT",
                f"подключён платный трафик ({tools}), но на сайте нет отдельных посадочных страниц под узкие категории дел",
            )
        return (
            "RUNNING_YANDEX_DIRECT",
            f"практика уже работает с платным привлечением ({tools})",
        )

    # 3. Карточки в 2ГИС / Яндекс Картах (нет сайта или пустая карточка при хорошем рейтинге)
    if "NO_WEBSITE" in by_code:
        return (
            "NO_WEBSITE",
            "у практики есть карточка в справочнике/реестре, но не привязан собственный сайт для приёма обращений из Яндекса",
        )
    if "UNCLAIMED_OR_EMPTY_CARD" in by_code:
        return (
            "UNCLAIMED_OR_EMPTY_CARD",
            "карточка в Яндекс Картах / 2ГИС заполнена частично (теряется часть локальных обращений по городу)",
        )

    # 4. Отсутствие посадочных страниц под практики
    if "MISSING_PRACTICE_PAGES" in by_code:
        return (
            "MISSING_PRACTICE_PAGES",
            "направления практики на сайте собраны на общей странице без отдельных разделов под узкие запросы в Яндексе",
        )

    # 5. Отсутствие Яндекс.Метрики
    if "NO_YANDEX_METRIKA" in by_code:
        return (
            "NO_YANDEX_METRIKA",
            "на сайте не установлен счётчик Яндекс.Метрики, из-за чего не видно конверсию обращений по направлениям",
        )

    # 6. Риски 38-ФЗ / КПЭА на сайте
    if "COMPLIANCE_RISK_ON_SITE" in by_code:
        detail = by_code["COMPLIANCE_RISK_ON_SITE"].get("value_text") or "рискованные формулировки"
        return (
            "COMPLIANCE_RISK_ON_SITE",
            f"на сайте замечены формулировки, чувствительные для модерации Директа и ст. 17 КПЭА ({detail})",
        )

    # 7. Быстрая связь в мессенджере или прозрачность
    if "NO_FAST_MESSENGER_CTA" in by_code:
        return (
            "NO_FAST_MESSENGER_CTA",
            "на сайте нет быстрого перехода в мессенджер со смартфона для первичных обращений доверителей",
        )
    if "WEAK_TRUST_TRANSPARENCY" in by_code:
        return (
            "WEAK_TRUST_TRANSPARENCY",
            "на сайте не вынесены на видное место реестровые номера и структура судебной практики",
        )

    return ("", "")


def company_fit(
    company: dict,
    prospects: list[dict],
    observations: list[dict],
    config: FridayConfig,
) -> tuple[int, list[str]]:
    """Рассчитать `(score, reasons)` для одной юридической практики."""
    dnc = config.compliance.do_not_contact
    hit = match_do_not_contact(
        domain=company.get("domain") or "",
        telegram=company.get("telegram") or "",
        entries=dnc,
    )
    if hit:
        return 0, [f"в стоп-листе ({hit})"]

    score = 0
    reasons: list[str] = []

    # 1. Профиль и география (до 25)
    target_cities = {c.strip().lower() for c in config.icp.cities if c.strip()}
    comp_city = (company.get("city") or "").strip()
    if comp_city and comp_city.lower() in target_cities:
        score += 13
        reasons.append(f"город: {comp_city}")
    elif comp_city:
        score += 6

    target_forms = {f.strip().lower() for f in config.icp.legal_forms if f.strip()}
    lform = (company.get("legal_form") or "").strip()
    if lform and any(tf in lform.lower() or lform.lower() in tf for tf in target_forms):
        score += 12
        reasons.append(f"форма: {lform}")
    elif lform:
        score += 6

    # 2. ЛПР (до 25)
    live_p = [p for p in prospects if p.get("status") not in ("opted_out", "skipped")]
    named = [p for p in live_p if (p.get("first_name") or "").strip()]
    top_roles = {"chairman", "managing_partner", "founder", "senior_partner", "partner", "head_of_practice", "solo_advocate"}
    dm = [p for p in named if (p.get("role_kind") or "") in top_roles]
    if dm:
        score += WEIGHT_DECISION_MAKER
        fio = f"{dm[0].get('last_name', '')} {dm[0].get('first_name', '')}".strip()
        reasons.append(f"ЛПР: {fio} ({dm[0].get('title', 'Адвокат')})")
    elif named:
        score += 15
        reasons.append("именной контакт")
    elif live_p:
        score += 6
        reasons.append("общий контакт практики")

    # 3. Каналы для ручного контакта (до 20)
    has_tg = bool(company.get("telegram")) or any(p.get("telegram") for p in live_p)
    has_email = any(p.get("email") for p in live_p)
    has_form = any(o.get("signal_code") == "CONTACT_FORM_URL" for o in observations)
    if has_tg and has_email:
        score += 20
        reasons.append("есть Telegram и рабочий email")
    elif has_tg:
        score += 18
        reasons.append("есть публичный Telegram")
    elif has_email:
        score += 15
        reasons.append("есть рабочий email")
    elif has_form or company.get("phone"):
        score += 8
        reasons.append("форма на сайте / телефон")

    # 4. Сигналы точек роста (до 30)
    codes = {o.get("signal_code") for o in observations}
    sig_pts = 0
    for o in observations:
        if o.get("signal_code") == "YANDEX_SERP_RANK":
            r = o.get("value_num")
            if r is not None and 11 <= r <= 30:
                sig_pts += 18
                reasons.append(f"Яндекс #{int(r)} (зона роста 11–30)")
                break
    if "RUNNING_YANDEX_DIRECT" in codes:
        sig_pts += 14
        reasons.append("активен платный трафик/коллтрекинг")
    if "MISSING_PRACTICE_PAGES" in codes:
        sig_pts += 10
        reasons.append("нет посадочных под узкие практики")
    if "NO_YANDEX_METRIKA" in codes:
        sig_pts += 8
        reasons.append("нет Яндекс.Метрики")
    if "UNCLAIMED_OR_EMPTY_CARD" in codes or "NO_WEBSITE" in codes:
        sig_pts += 10
        reasons.append("точка роста в карточке 2ГИС/Яндекс Карт")
    if "COMPLIANCE_RISK_ON_SITE" in codes:
        sig_pts += 8
        reasons.append("риски ФЗ-38/КПЭА на сайте")

    score += min(WEIGHT_GROWTH_SIGNAL, sig_pts)
    return min(100, score), reasons
