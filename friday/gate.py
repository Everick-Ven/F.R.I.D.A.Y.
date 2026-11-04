"""Детерминированный комплаенс-фильтр и проверка качества черновиков (Gate) для рынка РФ.

Проверяет каждый черновик в Python-коде перед показом человеку:
1. Общие правила ФЗ № 38-ФЗ «О рекламе» (ст. 5 — запрет голословных «№1 / лучший» и обманных 100%-гарантий).
2. Отраслевые правила для регулируемых ниш (`compliance.regulated_niche`:
   `none` для обычного бизнеса, либо `medicine` ст. 24 ФЗ-38, `finance` ст. 28 ФЗ-38, `legal_advocates` ст. 17 КПЭА).
3. Анти-спам и защита от русскоязычных ИИ-штампов («доброго времени суток», «взрывной рост» и т.д.).
4. Стоп-лист `do_not_contact` — домены (включая кириллические `.рф`), email и `@telegram`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# 1. Универсальные правила ФЗ-38 «О рекламе» (ст. 5) для любой ниши в РФ
FZ38_BANNED_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pat, re.I), reason)
    for pat, reason in (
        (
            r"\b100\s*%\s*(?:гарант\w*|результат\w*|окупаемост\w*|успех\w*)",
            "ФЗ-38 (ст. 5): недопустимое обещание 100% гарантии результата",
        ),
        (
            r"гарантир\w+\s+(?:удвоен\w*|рост\s+прибыли|рост\s+выручки|поток\s+клиентов\s+за)",
            "ФЗ-38 / анти-спам: недопустимы заведомо невыполнимые гарантии роста выручки/потока",
        ),
        (
            r"\b(?:лучш(?:ее|ий|ая|ие)|номер\s*один|№\s*1|#\s*1|единственн\w+)\s+(?:в\s+россии|на\s+рынке|компани\w*|сервис\w*|агентств\w*)",
            "ФЗ-38 (ст. 5): некорректное превосходное сравнение («лучший / №1») без подтверждения",
        ),
    )
)

# 2. Специальные правила для регулируемых ниш (срабатывают ТОЛЬКО если выбрана соответствующая ниша)
REGULATED_NICHE_PATTERNS: dict[str, tuple[tuple[re.Pattern, str], ...]] = {
    "medicine": tuple(
        (re.compile(pat, re.I), reason)
        for pat, reason in (
            (
                r"гарантир\w+\s+(?:излечени\w*|выздоровлени\w*|отсутстви\w*\s+побочн)",
                "ФЗ-38 (ст. 24 — Медицина): запрещены гарантии положительного действия и безопасности медуслуг",
            ),
        )
    ),
    "finance": tuple(
        (re.compile(pat, re.I), reason)
        for pat, reason in (
            (
                r"гарантир\w+\s+доходност\w*",
                "ФЗ-38 (ст. 28 — Финансы): запрещено обещать гарантированную доходность в будущем",
            ),
        )
    ),
    "legal_advocates": tuple(
        (re.compile(pat, re.I), reason)
        for pat, reason in (
            (
                r"(?:накрут\w*|куп\w*|заказ\w*)\s+(?:\w+\s+){0,2}отзыв\w*",
                "КПЭА (ст. 17): недопустимо предлагать покупку или накрутку отзывов",
            ),
            (
                r"размест\w+\s+отзыв\w+\s+(?:клиентов|доверителей)\s+на\s+(?:вашем\s+)?сайте",
                "КПЭА (ст. 17 п. 2): размещение отзывов доверителей на сайте адвоката несёт дисциплинарный риск",
            ),
            (
                r"(?:обойд\w+|разгром\w+|лучше\s+всех)\s+других\s+адвокатов",
                "КПЭА (ст. 17 п. 3): запрещены сравнения с другими адвокатами",
            ),
        )
    ),
}

# 3. Русскоязычные ИИ-клише и спам-маркеры для любой B2B-ниши
AI_AND_SPAM_PHRASES: tuple[str, ...] = (
    "доброго времени суток",
    "надеюсь, это письмо застанет вас",
    "надеюсь, у вас все хорошо",
    "надеюсь, у вас всё хорошо",
    "спешу предложить",
    "взаимовыгодное сотрудничество",
    "в современном динамично развивающемся мире",
    "в современном мире",
    "вывести на новый уровень",
    "вывести бизнес на новый уровень",
    "взрывной рост",
    "уникальное предложение",
    "эксклюзивное предложение",
    "только сегодня",
    "успейте заказать",
    "синергия",
    "я ии-ассистент",
    "как языковая модель",
)

FAKE_REPLY_SUBJECT_RE = re.compile(r"^\s*(?:re|fwd|отв|пересл)\s*:", re.I)
MERGE_TAG_RE = re.compile(r"\{\{\s*[\wа-яА-ЯёЁ]+\s*\}\}")
URL_RE = re.compile(r"https?://\S+", re.I)
_INVISIBLE_RE = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff]")
_EMAIL_LOCAL_RE = re.compile(r"^[a-z0-9._%+'-]+$")
_TG_HANDLE_RE = re.compile(r"^@[a-z0-9_]{3,64}$")


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.ok

    def __bool__(self) -> bool:
        return self.ok


def _clean_text(raw: object) -> str:
    text = unicodedata.normalize("NFKC", str(raw or ""))
    return _INVISIBLE_RE.sub("", text).replace("\u3002", ".")


def normalize_domain_idna(value: str) -> str:
    """Нормализовать домен (включая кириллические `.рф`) в ASCII punycode без `www.`."""
    host = _clean_text(value).strip().lower()
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", host)
    host = re.split(r"[/?#:]", host, maxsplit=1)[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return ""
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if re.fullmatch(r"[\d.]+", ascii_host):
        return ""
    return ascii_host


def display_domain(value: str) -> str:
    """Человекочитаемое имя домена (декодирует `xn--...` обратно в `.рф`)."""
    ascii_host = normalize_domain_idna(value)
    if not ascii_host:
        return ""
    try:
        return ascii_host.encode("ascii").decode("idna")
    except UnicodeError:
        return ascii_host


def normalize_dnc_entry(raw: object) -> str:
    """Нормализовать запись стоп-листа: email, `@telegram` или домен (в т.ч. `.рф`)."""
    entry = _clean_text(raw).strip().lower()
    if not entry:
        return ""
    if entry.startswith("https://t.me/") or entry.startswith("http://t.me/") or entry.startswith("t.me/"):
        handle = entry.split("t.me/", 1)[1].split("/")[0].split("?")[0].strip("@")
        if handle:
            return f"@{handle}"
    if entry.startswith("@") and "." not in entry:
        return entry if _TG_HANDLE_RE.match(entry) else ""
    entry = re.sub(r"^(?:[a-z][a-z0-9+.-]*://|mailto:)", "", entry)
    entry = re.split(r"[/?#]", entry, maxsplit=1)[0]
    entry = re.sub(r"^(?:\*@|\*\.|@)", "", entry)
    if "@" in entry:
        local, _, dom = entry.rpartition("@")
        ascii_dom = normalize_domain_idna(dom)
        if not ascii_dom or not _EMAIL_LOCAL_RE.match(local):
            return ""
        return f"{local}@{ascii_dom}"
    return normalize_domain_idna(entry)


def normalize_dnc_list(items: object) -> list[str]:
    if isinstance(items, str):
        raw_items = [x for x in re.split(r"[\s,;]+", items) if x.strip()]
    elif isinstance(items, (list, tuple, set)):
        raw_items = list(items)
    else:
        raise ValueError("do_not_contact должен быть списком строк")

    out: list[str] = []
    for item in raw_items:
        norm = normalize_dnc_entry(item)
        if not norm:
            raise ValueError(f"Некорректная запись в do_not_contact: {item!r}")
        if norm not in out:
            out.append(norm)
    return out


def match_do_not_contact(
    *,
    domain: str = "",
    email: str = "",
    telegram: str = "",
    entries: list[str] | tuple[str, ...] | None = None,
) -> str:
    if not entries:
        return ""
    norm_dom = normalize_domain_idna(domain)
    norm_email = normalize_dnc_entry(email) if email and "@" in email else ""
    norm_tg = normalize_dnc_entry(telegram) if telegram else ""

    email_local, _, email_host = norm_email.rpartition("@") if norm_email else ("", "", "")
    base_local = email_local.split("+", 1)[0] if email_local else ""

    for raw in entries:
        rule = normalize_dnc_entry(raw)
        if not rule:
            continue
        if rule.startswith("@"):
            if norm_tg and norm_tg == rule:
                return rule
        elif "@" in rule:
            r_local, _, r_host = rule.rpartition("@")
            if email_host == r_host and r_local in (email_local, base_local):
                return rule
        else:
            for candidate_host in (norm_dom, email_host):
                if candidate_host and (candidate_host == rule or candidate_host.endswith("." + rule)):
                    return rule
    return ""


def check_draft(
    *,
    channel: str,
    subject: str,
    body: str,
    config=None,
    domain: str = "",
    email: str = "",
    telegram: str = "",
) -> GateResult:
    """Проверить сгенерированный черновик перед показом человеку."""
    reasons: list[str] = []
    warnings: list[str] = []

    comp = getattr(config, "compliance", None)
    dnc = getattr(comp, "do_not_contact", None) or []
    enforce_fz38 = True if comp is None else bool(comp.enforce_fz38)
    reg_niche = getattr(comp, "regulated_niche", "none") if comp else "none"
    max_tg_words = getattr(comp, "max_telegram_words", 85) if comp else 85
    max_email_words = getattr(comp, "max_email_words", 140) if comp else 140

    hit_dnc = match_do_not_contact(domain=domain, email=email, telegram=telegram, entries=dnc)
    if hit_dnc:
        reasons.append(f"Контакт или домен находится в стоп-листе (do_not_contact: {hit_dnc})")

    clean_body = _clean_text(body).strip()
    clean_subj = _clean_text(subject).strip()
    combined = f"{clean_subj}\n{clean_body}"
    low_combined = combined.lower().replace("ё", "е")

    if not clean_body:
        reasons.append("Пустой текст черновика")
        return GateResult(ok=False, reasons=reasons)

    if channel == "email":
        if not clean_subj:
            reasons.append("Для email-черновика обязательна тема письма")
        elif len(clean_subj) > 90:
            reasons.append(f"Тема письма слишком длинная ({len(clean_subj)} симв., лимит 90)")
        elif FAKE_REPLY_SUBJECT_RE.match(clean_subj):
            reasons.append("Обманный префикс Re:/Fwd:/Отв: в теме первого письма запрещён")

    if MERGE_TAG_RE.search(combined):
        reasons.append("В тексте остались неподставленные шаблонные переменные {{...}}")

    words = clean_body.split()
    word_limit = max_email_words if channel == "email" else max_tg_words
    if len(words) > word_limit:
        reasons.append(f"Превышен лимит длины для канала '{channel}': {len(words)} слов (макс. {word_limit})")

    if "!" in clean_body:
        reasons.append("Восклицательные знаки в первом деловом касании запрещены")

    links = URL_RE.findall(clean_body)
    if len(links) > 1:
        reasons.append(f"Слишком много ссылок в первом касании ({len(links)}, максимум 0–1)")
    elif len(links) == 1 and channel in ("telegram", "whatsapp", "tenchat"):
        warnings.append("Ссылка в первом сообщении в мессенджере может восприниматься настороженно")

    if enforce_fz38:
        for pat, msg in FZ38_BANNED_PATTERNS:
            if pat.search(combined):
                reasons.append(msg)

    for pat, msg in REGULATED_NICHE_PATTERNS.get(reg_niche, ()):
        if pat.search(combined):
            reasons.append(msg)

    for phrase in AI_AND_SPAM_PHRASES:
        if phrase.lower().replace("ё", "е") in low_combined:
            reasons.append(f"Обнаружен спам/ИИ-штамп: «{phrase}»")

    q_count = clean_body.count("?")
    if q_count == 0:
        warnings.append("В сообщении нет завершающего вопроса (рекомендуется 1 спокойный вопрос)")
    elif q_count > 2:
        reasons.append(f"Слишком много вопросов в одном сообщении ({q_count})")

    return GateResult(ok=len(reasons) == 0, reasons=reasons, warnings=warnings)


def is_suppressed(
    *,
    company_domain: str = "",
    prospect_email: str = "",
    prospect_telegram: str = "",
    config=None,
) -> tuple[bool, str]:
    comp = getattr(config, "compliance", None)
    dnc = getattr(comp, "do_not_contact", None) or []
    hit = match_do_not_contact(
        domain=company_domain,
        email=prospect_email,
        telegram=prospect_telegram,
        entries=dnc,
    )
    return (bool(hit), hit)


def validate_draft(
    *,
    channel: str,
    subject: str,
    body: str,
    company_domain: str = "",
    prospect_email: str = "",
    prospect_telegram: str = "",
    config=None,
) -> GateResult:
    return check_draft(
        channel=channel,
        subject=subject,
        body=body,
        config=config,
        domain=company_domain,
        email=prospect_email,
        telegram=prospect_telegram,
    )
