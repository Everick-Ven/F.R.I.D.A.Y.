"""PEOPLE — извлечение ЛПР и адвокатов с русскоязычных сайтов практик ($0, без LLM).

Распознаёт:
- полные русские ФИО в порядке «Фамилия Имя Отчество» и «Имя Отчество Фамилия»
  с морфологической проверкой отчества (`-ович/-евич/-ич`, `-овна/-евна/-ична`);
- пары «Имя Фамилия» / «Фамилия Имя» рядом с юридической должностью;
- российские юридические должности ЛПР (Управляющий партнёр, Председатель коллегии,
  Старший партнёр, Партнёр, Руководитель практики, Учредитель адвокатского кабинета, Адвокат);
- реестровые номера Минюста РФ (`77/12345`) рядом с карточкой адвоката.
"""

from __future__ import annotations

import html as html_lib
import json
import re

PATRONYMIC_RE = re.compile(
    r"^[А-ЯЁ][а-яё]{2,}(?:ович|евич|ёвич|ич|овна|евна|ёвна|ична|инична)$"
)

SURNAME_HINT_RE = re.compile(
    r"^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?(?:ов|ова|ев|ева|ёв|ёва|ин|ина|ын|ына|"
    r"ский|ская|цкий|цкая|ской|цкой|ой|ая|ко|ук|юк|чук|ич|их|ых|ец|ак|як|ан|ян|ман|берг|штейн)$"
)

COMMON_FIRST_NAMES_RU = frozenset("""
александр алексей андрей антон аркадий арсений артем артём артур борис вадим валентин валерий
василий виктор виталий владимир владислав вячеслав геннадий георгий глеб григорий даниил данил
денис дмитрий евгений егор иван игорь илья кирилл константин лев леонид максим марат марк
матвей михаил никита николай олег павел петр пётр роман руслан сергей станислав степан тимур
федор фёдор филипп эдуард юрий ярослав
алена алёна алина алла анастасия ангелина анна валентина валерия варвара вера вероника виктория
галина дарья диана евгения екатерина елена елизавета жанна зинаида инна ирина карина кристина
ксения лариса лидия любовь людмила маргарита марина мария надежда наталья наталия нина оксана
олеся ольга полина раиса светлана софия софья тамара татьяна ульяна юлия яна
""".split())

NON_PERSON_RU_WORDS = frozenset("""
адвокат адвокаты адвокатская адвокатское адвокатский коллегия бюро кабинет палата юрист юристы
юридическая юридические юридический правовая правовой право защита помощь услуги практика практики
дела делам споры спорам уголовные уголовный гражданские семейные наследственные арбитражные арбитраж
банкротство недвижимость налоговые трудовые корпоративные сопровождение представительство суд суде
москва московская санкт петербург екатеринбург новосибирск казань россия рф область край город
главная контакты команда партнеры партнёры специалисты компания о нас отзывы цены стоимость статьи
новости блог политика конфиденциальности соглашение оферта карта сайта поиск меню телефон адрес
председатель управляющий старший партнер партнёр руководитель директор основатель учредитель
почетный почётный президент вице стажер стажёр помощник секретарь консультация бесплатно онлайн
""".split())

_CYR_TOKEN_RE = re.compile(r"^[А-ЯЁ][а-яё]{1,25}(?:-[А-ЯЁ][а-яё]{1,25})?$")

ROLE_PATTERNS_RU: tuple[tuple[re.Pattern, str, int], ...] = (
    (re.compile(r"председател\w*\s+(?:коллегии|президиума)", re.I), "chairman", 100),
    (re.compile(r"управляющ\w*\s+партн[её]р", re.I), "managing_partner", 100),
    (re.compile(r"учредител\w*\s+(?:адвокатского\s+)?кабинета|основател\w*", re.I), "founder", 95),
    (re.compile(r"руководител\w*\s+(?:адвокатского\s+)?(?:бюро|коллегии|офиса)", re.I), "managing_partner", 95),
    (re.compile(r"старш\w*\s+партн[её]р", re.I), "senior_partner", 90),
    (re.compile(r"(?<![а-яё])партн[её]р(?![а-яё])", re.I), "partner", 85),
    (re.compile(r"руководител\w*\s+(?:[\wа-яё-]+\s+){0,2}практик\w*", re.I), "head_of_practice", 80),
    (re.compile(r"директор\s+по\s+(?:развитию|маркетингу)", re.I), "marketing", 75),
    (re.compile(r"заместител\w*\s+председателя", re.I), "deputy_chairman", 75),
    (re.compile(r"(?<![а-яё])адвокат(?![а-яё])", re.I), "solo_advocate", 65),
    (re.compile(r"ведущ\w*\s+юрист|руководител\w*\s+юридическ", re.I), "lawyer_lead", 50),
)

SUBORDINATE_RU_RE = re.compile(r"\b(?:помощник|стаж[её]р|секретарь|паралигал|курьер|студент)\b", re.I)
REGISTRY_NEAR_RE = re.compile(r"(?:реестр[^\d]{0,25}|№\s*)(\d{2}/\d{1,6})\b", re.I)


def _valid_cyr_word(token: str) -> bool:
    clean = token.strip(".,«»\"'() ")
    if not _CYR_TOKEN_RE.match(clean):
        return False
    low = clean.lower().replace("ё", "е")
    if low in NON_PERSON_RU_WORDS:
        return False
    return True


def split_russian_fio(raw_text: str) -> tuple[str, str, str] | None:
    """Разобрать строку ФИО в кортеж `(last_name, first_name, middle_name)` или вернуть `None`.

    Поддерживает:
    - «Волков Алексей Сергеевич» -> ('Волков', 'Алексей', 'Сергеевич')
    - «Алексей Сергеевич Волков» -> ('Волков', 'Алексей', 'Сергеевич')
    - «Алексей Волков» / «Волков Алексей» -> ('Волков', 'Алексей', '')
    """
    text = html_lib.unescape(raw_text or "").replace("\xa0", " ")
    text = re.sub(r"(?i)^\s*(?:адвокат|партн[её]р|юрист|к\.ю\.н\.|д\.ю\.н\.)\s*,?\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.|-–—«»\"'")
    tokens = [t.strip(",") for t in text.split() if t.strip(",")]

    if not (2 <= len(tokens) <= 3):
        return None
    if not all(_valid_cyr_word(t) for t in tokens):
        return None

    if len(tokens) == 3:
        t0, t1, t2 = tokens
        # Вариант 1: Фамилия Имя Отчество (отчество на 3-м месте)
        if PATRONYMIC_RE.match(t2) and not PATRONYMIC_RE.match(t1):
            return t0, t1, t2
        # Вариант 2: Имя Отчество Фамилия (отчество на 2-м месте)
        if PATRONYMIC_RE.match(t1) and not PATRONYMIC_RE.match(t2):
            return t2, t0, t1
        return None

    # 2 слова: требуем, чтобы хотя бы одно слово было известным русским именем или имело суффикс фамилии
    t0, t1 = tokens
    t0_low = t0.lower().replace("ё", "е")
    t1_low = t1.lower().replace("ё", "е")
    if t0_low in COMMON_FIRST_NAMES_RU and t1_low not in COMMON_FIRST_NAMES_RU:
        return t1, t0, ""
    if t1_low in COMMON_FIRST_NAMES_RU and t0_low not in COMMON_FIRST_NAMES_RU:
        return t0, t1, ""
    if SURNAME_HINT_RE.match(t0) and not SURNAME_HINT_RE.match(t1):
        return t0, t1, ""
    if SURNAME_HINT_RE.match(t1) and not SURNAME_HINT_RE.match(t0):
        return t1, t0, ""
    return None


def classify_russian_role(context_text: str) -> tuple[str, str, int]:
    """Вернуть `(display_title, role_kind, rank)` по тексту рядом с ФИО или `('', '', 0)`."""
    clean = re.sub(r"\s+", " ", (context_text or "")).strip()
    if not clean or SUBORDINATE_RU_RE.search(clean[:120]):
        return "", "", 0
    window = clean[:180]
    for regex, kind, rank in ROLE_PATTERNS_RU:
        m = regex.search(window)
        if m:
            snippet = m.group(0).strip()
            # Делаем первую букву заглавной для аккуратного отображения
            display = snippet[:1].upper() + snippet[1:]
            return display, kind, rank
    return "", "", 0


def _strip_scripts(html: str) -> str:
    html = re.sub(r"(?is)<script\b.*?</script>", " ", html)
    html = re.sub(r"(?is)<style\b.*?</style>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    return html


def extract_people_from_html(html: str, page_url: str = "") -> list[dict]:
    """Извлечь список ЛПР и адвокатов из HTML страницы команды / о коллегии / главной.

    Каждый элемент содержит:
        last_name, first_name, middle_name, title, role_kind, rank,
        registry_number, source, evidence_url
    """
    if not html:
        return []

    found: dict[tuple[str, str], dict] = {}

    # 1. JSON-LD Schema.org (Attorney / LegalService / Person / founder / employee)
    for m in re.finditer(
        r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html
    ):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in list(nodes):
            if isinstance(node, dict) and "@graph" in node and isinstance(node["@graph"], list):
                nodes.extend(node["@graph"])
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for rel_key in ("founder", "founders", "employee", "employees", "member", "members"):
                rel = node.get(rel_key)
                if isinstance(rel, dict):
                    nodes.append(rel)
                elif isinstance(rel, list):
                    nodes.extend(x for x in rel if isinstance(x, dict))
            ntype = str(node.get("@type") or "")
            if any(k in ntype for k in ("Person", "Attorney")):
                fio = split_russian_fio(str(node.get("name") or ""))
                if fio:
                    job = str(node.get("jobTitle") or "Адвокат")
                    disp_title, kind, rank = classify_russian_role(job)
                    if not disp_title:
                        disp_title, kind, rank = "Адвокат", "solo_advocate", 65
                    key = (fio[0].lower(), fio[1].lower())
                    found[key] = {
                        "last_name": fio[0],
                        "first_name": fio[1],
                        "middle_name": fio[2],
                        "title": disp_title,
                        "role_kind": kind,
                        "rank": rank,
                        "registry_number": "",
                        "source": "schema",
                        "evidence_url": page_url,
                    }

    # 2. Заголовки карточек и блоки команды (<h1>..<h4>, <strong>, <b>, <div class="...name...">)
    clean_html = _strip_scripts(html)
    tag_pattern = re.compile(
        r"(?is)<(?P<tag>h[1-5]|strong|b|span|div|p|a)\b[^>]*>(?P<inner>[^<]{5,90})</(?P=tag)>"
    )
    for m in tag_pattern.finditer(clean_html):
        candidate_text = html_lib.unescape(m.group("inner")).strip()
        fio = split_russian_fio(candidate_text)
        if not fio:
            continue

        # Смотрим контекст до (80 симв.) и после (260 симв.) имени на наличие должности и номера реестра
        before_raw = clean_html[max(0, m.start() - 160) : m.start()]
        after_raw = clean_html[m.end() : m.end() + 340]
        context_plain = html_lib.unescape(
            re.sub(r"(?s)<[^>]+>", " ", before_raw + " " + after_raw)
        )
        disp_title, kind, rank = classify_russian_role(context_plain)

        # Полное ФИО с отчеством на юридическом сайте само по себе сильный маркер адвоката,
        # а для 2 слов должность рядом строго обязательна.
        if not disp_title:
            if fio[2]:
                disp_title, kind, rank = "Адвокат", "solo_advocate", 60
            else:
                continue

        reg_match = REGISTRY_NEAR_RE.search(context_plain)
        reg_no = reg_match.group(1) if reg_match else ""

        key = (fio[0].lower(), fio[1].lower())
        prev = found.get(key)
        if prev is None or rank > prev["rank"] or (not prev["middle_name"] and fio[2]):
            found[key] = {
                "last_name": fio[0],
                "first_name": fio[1],
                "middle_name": fio[2] or (prev["middle_name"] if prev else ""),
                "title": disp_title,
                "role_kind": kind,
                "rank": rank,
                "registry_number": reg_no or (prev["registry_number"] if prev else ""),
                "source": "card",
                "evidence_url": page_url,
            }

    # 3. Фразовые конструкции («Адвокатский кабинет Иванова Петра Сергеевича» / «Управляющий партнёр — ...»)
    prose_text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", clean_html))
    prose_re = re.compile(
        r"(?P<role>Управляющий\s+партн[её]р|Председатель\s+коллегии|Старший\s+партн[её]р|Адвокат)\s*"
        r"[:—–\-]?\s*(?P<fio>[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)"
    )
    for pm in prose_re.finditer(prose_text):
        fio = split_russian_fio(pm.group("fio"))
        if not fio:
            continue
        disp_title, kind, rank = classify_russian_role(pm.group("role"))
        key = (fio[0].lower(), fio[1].lower())
        if key not in found:
            found[key] = {
                "last_name": fio[0],
                "first_name": fio[1],
                "middle_name": fio[2],
                "title": disp_title or "Адвокат",
                "role_kind": kind or "solo_advocate",
                "rank": rank or 65,
                "registry_number": "",
                "source": "prose",
                "evidence_url": page_url,
            }

    people = sorted(found.values(), key=lambda x: (x["rank"], bool(x["middle_name"])), reverse=True)
    return people
