"""Детерминированный комплаенс-фильтр и проверка качества черновиков (Gate).

Инструкции в промпте для LLM — это пожелание, а не гарантия. Поэтому каждый
черновик F.R.I.D.A.Y. проходит жёсткую проверку в Python-коде по четырём контурам:
1. ФЗ № 38-ФЗ «О рекламе» (ст. 5 — запрет гарантий результата и голословных «№1 / лучший»).
2. Кодекс профессиональной этики адвоката — ст. 17 КПЭА (запрет предложений по накрутке отзывов,
   сравнению с другими адвокатами и обещаниям выигрыша дел).
3. Защита от спам-штампов и русскоязычных ИИ-клише («доброго времени суток», «взрывной рост» и т.п.).
4. Стоп-лист (do_not_contact) — домены (включая кириллические .рф), email и @telegram.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# 1. Нарушения принципов бюро и ст. 5 ФЗ-38 «О рекламе» (гарантии результата и превосходная степень)
FZ38_BANNED_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pat, re.I), reason)
    for pat, reason in (
        (
            r"гарантир\w+\s+(?:результат|поток|заявк\w*|клиент\w*|доверител\w*|выигрыш|окупаемост\w*|лид\w*)",
            "ФЗ-38 / позиционирование бюро: запрещены обещания и гарантии потока заявок или результата",
        ),
        (
            r"\b100\s*%\s*(?:гарант\w*|результат\w*|выигрыш\w*|успех\w*)",
            "ФЗ-38 (ст. 5): недопустимое обещание 100% гарантии результата",
        ),
        (
            r"\b(?:лучш(?:ее|ий|ая|ие)|номер\s*один|№\s*1|#\s*1|единственн\w+)\s+(?:агентств\w*|бюро|команд\w*|на\s+рынке)",
            "ФЗ-38 (ст. 5): некорректное превосходное сравнение без объективного подтверждения",
        ),
        (
            r"(?:удвоим|утроим|увеличим\s+в\s+\d+\s+раз\w*)\s+(?:выручк\w*|прибыл\w*|поток|доход\w*|количество\s+дел)",
            "Позиционирование бюро: запрещены инфоцыганские обещания кратного роста выручки",
        ),
    )
)

# 2. Риски нарушения ст. 17 КПЭА в предложениях адвокату
KPEA_BANNED_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pat, re.I), reason)
    for pat, reason in (
        (
            r"(?:накрут\w*|куп\w*|заказ\w*|напиш\w*)\s+(?:\w+\s+){0,2}отзыв\w*",
            "КПЭА (ст. 17) и УК/КоАП: недопустимо предлагать покупку или накрутку отзывов",
        ),
        (
            r"размест\w+\s+отзыв\w+\s+(?:клиентов|доверителей)\s+на\s+(?:вашем\s+)?сайте",
            "КПЭА (ст. 17 п. 2): отзывы других лиц о работе адвоката на его сайте несут дисциплинарный риск",
        ),
        (
            r"(?:обойд\w+|разгром\w+|лучше\s+всех)\s+других\s+адвокатов",
            "КПЭА (ст. 17 п. 3): недопустимы сравнения с другими адвокатами и их критика",
        ),
        (
            r"обеща\w+\s+(?:доверител\w+\s+)?выигрыш\w*\s+дел",
            "КПЭА (ст. 17 п. 4): запрещены заявления, вызывающие безосновательные надежды у доверителей",
        ),
    )
)

# 3. Русскоязычные ИИ-клише и спам-маркеры, убивающие доверие адвокатов
AI_AND_SPAM_PHRASES: tuple[str, ...] = (
    "доброго времени суток",
    "надеюсь, это письмо застанет вас",
    "надеюсь, у вас все хорошо",
    "надеюсь, у вас всё хорошо",
    "в современном динамично развивающемся мире",
    "в современном мире",
    "вывести на новый уровень",
    "вывести практику на новый уровень",
    "взрывной рост",
    "уникальное предложение",
    "эксклюзивное предложение",
    "только сегодня",
    "успейте заказать",
    "комплексное продвижение под ключ",
    "синергия",
    "инновационное решение",
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

    def __bool__(self) -> bool:
        return self.ok


def _clean_text(raw: object) -> str:
    text = unicodedata.normalize("NFKC", str(raw or ""))
    return _INVISIBLE_RE.sub("", text).replace("\u3002", ".")


def normalize_domain_idna(value: str) -> str:
    """Нормализовать домен (включая кириллические .рф домены) в ASCII punycode без www."""
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
    """Человекочитаемое представление домена (декодирует xn--... в кириллицу .рф для UI)."""
    ascii_host = normalize_domain_idna(value)
    if not ascii_host:
        return ""
    try:
        return ascii_host.encode("ascii").decode("idna")
    except UnicodeError:
        return ascii_host


def normalize_dnc_entry(raw: object) -> str:
    """Нормализовать одну запись стоп-листа: email, @telegram или домен (в т.ч. .рф)."""
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
    """Провалидировать и нормализовать список `do_not_contact`."""
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
    """Вернуть сработавшее правило из `do_not_contact` или пустую строку."""
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
    """Проверить сгенерированный черновик по правилам ФЗ-38, КПЭА, анти-спама и стоп-листа."""
    reasons: list[str] = []
    warnings: list[str] = []

    comp = getattr(config, "compliance", None)
    dnc = getattr(comp, "do_not_contact", None) or []
    enforce_fz38 = True if comp is None else bool(comp.enforce_fz38)
    enforce_kpea = True if comp is None else bool(comp.enforce_kpea)
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

    # Лимит слов по каналу
    words = clean_body.split()
    word_limit = max_email_words if channel == "email" else max_tg_words
    if len(words) > word_limit:
        reasons.append(f"Превышен лимит длины для канала '{channel}': {len(words)} слов (макс. {word_limit})")

    # Восклицательные знаки и ссылки в первом заходе
    if "!" in clean_body:
        reasons.append("Восклицательные знаки в первом деловом касании запрещены")

    links = URL_RE.findall(clean_body)
    if len(links) > 1:
        reasons.append(f"Слишком много ссылок в первом касании ({len(links)}, максимум 0–1)")
    elif len(links) == 1 and channel in ("telegram", "tenchat"):
        warnings.append("Ссылка в первом сообщении в мессенджере может восприниматься настороженно")

    # Проверка ФЗ-38 «О рекламе»
    if enforce_fz38:
        for pat, msg in FZ38_BANNED_PATTERNS:
            if pat.search(combined):
                reasons.append(msg)

    # Проверка ст. 17 КПЭА
    if enforce_kpea:
        for pat, msg in KPEA_BANNED_PATTERNS:
            if pat.search(combined):
                reasons.append(msg)

    # Проверка русскоязычных ИИ-штампов и спам-фраз
    for phrase in AI_AND_SPAM_PHRASES:
        norm_phrase = phrase.lower().replace("ё", "е")
        if norm_phrase in low_combined:
            reasons.append(f"Обнаружен спам/ИИ-штамп: «{phrase}»")

    # Не более одного вопросительного предложения
    q_count = clean_body.count("?")
    if q_count == 0:
        warnings.append("В сообщении нет завершающего вопроса (рекомендуется 1 спокойный вопрос)")
    elif q_count > 2:
        reasons.append(f"Слишком много вопросов в одном сообщении ({q_count})")

    return GateResult(ok=len(reasons) == 0, reasons=reasons, warnings=warnings)
