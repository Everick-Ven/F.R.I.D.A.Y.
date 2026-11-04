"""SQLite менеджер состояния F.R.I.D.A.Y.

Ключевые принципы (унаследованы из архитектуры Harvey):
- Режим WAL: веб-интерфейс и экспорт читают БД параллельно со сбором данных без блокировок.
- Линейные идемпотентные миграции через `PRAGMA user_version`.
- Модель наблюдений (`signal_codes` + `observations` + триггер `trg_observations_signal_known`):
  каждый факт о практике — это строка с `confidence`, `evidence_url` и временем наблюдения.
- Таблица `drafts` хранит персональные черновики и мини-разборы ТОЛЬКО для ручной проверки
  и личной отправки человеком (автоматической холодной рассылки нет).
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from friday.gate import normalize_domain_idna
from friday.models.company import Company
from friday.models.draft import Draft
from friday.models.prospect import Prospect
from friday.paths import DATA_DIR

DB_PATH = DATA_DIR / "friday.db"
BUSY_TIMEOUT_SECONDS = 30.0


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


MIGRATIONS: list[str] = [
    # ── v1: полная схема F.R.I.D.A.Y. для российского юридического рынка ──
    """
    CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        domain TEXT DEFAULT '',
        website TEXT DEFAULT '',
        legal_form TEXT DEFAULT '',
        practice_areas_json TEXT DEFAULT '[]',
        description TEXT DEFAULT '',
        city TEXT DEFAULT '',
        address TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        telegram TEXT DEFAULT '',
        whatsapp TEXT DEFAULT '',
        vk TEXT DEFAULT '',
        tenchat TEXT DEFAULT '',
        ymaps_url TEXT DEFAULT '',
        twogis_url TEXT DEFAULT '',
        registry_region TEXT DEFAULT '',
        external_id TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        tech_stack_json TEXT DEFAULT '[]',
        signals_json TEXT DEFAULT '[]',
        notes TEXT DEFAULT '',
        profile_checked_at TIMESTAMP DEFAULT NULL,
        prospects_checked_at TIMESTAMP DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_domain
        ON companies(domain) WHERE domain != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_external
        ON companies(external_id) WHERE external_id != '';
    CREATE INDEX IF NOT EXISTS idx_companies_city ON companies(city);

    CREATE TABLE IF NOT EXISTS prospects (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '' REFERENCES companies(id),
        last_name TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        middle_name TEXT DEFAULT '',
        title TEXT DEFAULT '',
        role_kind TEXT DEFAULT '',
        registry_number TEXT DEFAULT '',
        registry_status TEXT DEFAULT '',
        email TEXT DEFAULT '',
        email_source TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        telegram TEXT DEFAULT '',
        whatsapp TEXT DEFAULT '',
        tenchat TEXT DEFAULT '',
        vk TEXT DEFAULT '',
        company TEXT DEFAULT '',
        city TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        score INTEGER DEFAULT 0,
        personalization_notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_prospects_company_id ON prospects(company_id);
    CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
    CREATE INDEX IF NOT EXISTS idx_prospects_score ON prospects(score DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_registry
        ON prospects(registry_number) WHERE registry_number != '';

    CREATE TABLE IF NOT EXISTS signal_codes (
        code TEXT PRIMARY KEY,
        label TEXT DEFAULT '',
        description TEXT DEFAULT '',
        category TEXT DEFAULT '',
        value_type TEXT DEFAULT 'text',
        collector TEXT DEFAULT '',
        cost_note TEXT DEFAULT '',
        status TEXT DEFAULT 'proposed',
        confidence_floor REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        signal_code TEXT NOT NULL,
        collector TEXT DEFAULT '',
        value_num REAL,
        value_text TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        evidence_url TEXT DEFAULT '',
        observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        run_id TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_obs_company ON observations(company_id);
    CREATE INDEX IF NOT EXISTS idx_obs_signal ON observations(signal_code);
    CREATE INDEX IF NOT EXISTS idx_obs_company_signal
        ON observations(company_id, signal_code, observed_at);

    CREATE TRIGGER IF NOT EXISTS trg_observations_signal_known
    BEFORE INSERT ON observations
    FOR EACH ROW
    WHEN NEW.signal_code NOT IN (SELECT code FROM signal_codes)
    BEGIN
        SELECT RAISE(ABORT, 'unknown signal_code: not in signal_codes vocabulary');
    END;

    CREATE TABLE IF NOT EXISTS drafts (
        id TEXT PRIMARY KEY,
        prospect_id TEXT NOT NULL REFERENCES prospects(id),
        company_id TEXT DEFAULT '',
        channel TEXT DEFAULT 'telegram',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        audit_summary TEXT DEFAULT '',
        signal_used TEXT DEFAULT '',
        status TEXT DEFAULT 'pending_review',
        gate_notes_json TEXT DEFAULT '[]',
        brain_provider TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_drafts_prospect_channel
        ON drafts(prospect_id, channel);
    CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        stage TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        provider TEXT DEFAULT '',
        records INTEGER DEFAULT 0,
        cost_rub REAL DEFAULT 0.0,
        params_json TEXT DEFAULT '{}',
        error TEXT DEFAULT '',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS usage_events (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        provider TEXT DEFAULT 'claude',
        agent TEXT DEFAULT '',
        task TEXT DEFAULT '',
        model TEXT DEFAULT '',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        duration_ms INTEGER DEFAULT 0,
        is_error INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS agent_tasks (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,           -- scout_score | writer_draft
        status TEXT DEFAULT 'pending', -- pending | completed | cancelled
        payload_json TEXT NOT NULL,
        prompt_text TEXT NOT NULL,
        response_json TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status, created_at);

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
]


def _parse_json_list(raw: object) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        val = json.loads(str(raw))
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


class StateManager:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DB_PATH)

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    async def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            async with db.execute("PRAGMA user_version") as cur:
                (version,) = await cur.fetchone()
            for target, script in enumerate(MIGRATIONS, start=1):
                if version < target:
                    await db.executescript(script)
                    await db.execute(f"PRAGMA user_version = {target}")
                    await db.commit()

    # ── Settings KV ──

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._connect() as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
                return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        now = _utcnow_str()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            await db.commit()

    # ── Signal Vocabulary & Observations ──

    async def upsert_signal_code(
        self,
        code: str,
        *,
        label: str,
        description: str,
        category: str,
        value_type: str,
        collector: str,
        cost_note: str,
        confidence_floor: float = 0.0,
    ) -> None:
        now = _utcnow_str()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO signal_codes(
                    code, label, description, category, value_type, collector,
                    cost_note, status, confidence_floor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    label = excluded.label,
                    description = excluded.description,
                    category = excluded.category,
                    value_type = excluded.value_type,
                    collector = excluded.collector,
                    cost_note = excluded.cost_note,
                    confidence_floor = excluded.confidence_floor,
                    updated_at = excluded.updated_at
                """,
                (
                    code, label, description, category, value_type,
                    collector, cost_note, confidence_floor, now, now,
                ),
            )
            await db.commit()

    async def get_signal_code(self, code: str) -> dict | None:
        async with self._connect() as db:
            async with db.execute("SELECT * FROM signal_codes WHERE code = ?", (code,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def list_signal_codes(self, status: str | None = None) -> list[dict]:
        async with self._connect() as db:
            if status:
                sql = "SELECT * FROM signal_codes WHERE status = ? ORDER BY category, code"
                params: tuple = (status,)
            else:
                sql = "SELECT * FROM signal_codes ORDER BY category, code"
                params = ()
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def confirmed_signal_codes(self, collector: str | None = None) -> set[str]:
        async with self._connect() as db:
            if collector:
                sql = "SELECT code FROM signal_codes WHERE status = 'confirmed' AND collector = ?"
                params: tuple = (collector,)
            else:
                sql = "SELECT code FROM signal_codes WHERE status = 'confirmed'"
                params = ()
            async with db.execute(sql, params) as cur:
                return {r["code"] for r in await cur.fetchall()}

    async def set_signal_status(self, code: str, status: str) -> bool:
        if status not in ("proposed", "confirmed", "rejected"):
            raise ValueError(f"Недопустимый статус сигнала: {status}")
        now = _utcnow_str()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE signal_codes SET status = ?, updated_at = ? WHERE code = ?",
                (status, now, code),
            )
            await db.commit()
            return cur.rowcount > 0

    async def record_observation(
        self,
        *,
        signal_code: str,
        company_id: str = "",
        prospect_id: str = "",
        collector: str = "",
        value_num: float | None = None,
        value_text: str = "",
        confidence: float = 1.0,
        evidence_url: str = "",
        run_id: str = "",
        require_confirmed: bool = True,
    ) -> str | None:
        """Записать наблюдение (если сигнал подтверждён пользователем и проходит порог confidence)."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT status, confidence_floor FROM signal_codes WHERE code = ?",
                (signal_code,),
            ) as cur:
                sig = await cur.fetchone()
            if sig is None:
                # Даём сработать триггеру БД для явной ошибки словаря
                obs_id = _new_id()
                await db.execute(
                    "INSERT INTO observations(id, signal_code) VALUES (?, ?)",
                    (obs_id, signal_code),
                )
                return None

            if require_confirmed and sig["status"] != "confirmed":
                return None
            if confidence < float(sig["confidence_floor"] or 0.0):
                return None

            obs_id = _new_id()
            await db.execute(
                """
                INSERT INTO observations(
                    id, company_id, prospect_id, signal_code, collector,
                    value_num, value_text, confidence, evidence_url, observed_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obs_id, company_id, prospect_id, signal_code, collector,
                    value_num, value_text, confidence, evidence_url, _utcnow_str(), run_id,
                ),
            )
            await db.commit()
            return obs_id

    async def get_observations(
        self,
        *,
        company_id: str | None = None,
        prospect_id: str | None = None,
        signal_code: str | None = None,
    ) -> list[dict]:
        where, params = [], []
        if company_id is not None:
            where.append("company_id = ?")
            params.append(company_id)
        if prospect_id is not None:
            where.append("prospect_id = ?")
            params.append(prospect_id)
        if signal_code is not None:
            where.append("signal_code = ?")
            params.append(signal_code)
        sql = "SELECT * FROM observations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY observed_at DESC"
        async with self._connect() as db:
            async with db.execute(sql, tuple(params)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def cohort(
        self,
        require: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[Company]:
        """Построить когорту практик пересечением подтверждённых сигналов (SQL set intersection)."""
        require = [c for c in (require or []) if c]
        exclude = [c for c in (exclude or []) if c]
        where, params = ["1=1"], []
        for code in require:
            where.append(
                "EXISTS (SELECT 1 FROM observations o WHERE o.company_id = c.id AND o.signal_code = ?)"
            )
            params.append(code)
        for code in exclude:
            where.append(
                "NOT EXISTS (SELECT 1 FROM observations o WHERE o.company_id = c.id AND o.signal_code = ?)"
            )
            params.append(code)
        sql = f"SELECT * FROM companies c WHERE {' AND '.join(where)} ORDER BY c.created_at DESC"
        async with self._connect() as db:
            async with db.execute(sql, tuple(params)) as cur:
                return [self._company_from_row(r) for r in await cur.fetchall()]

    # ── Companies (Адвокатские образования и практики) ──

    @staticmethod
    def _company_from_row(row: aiosqlite.Row) -> Company:
        d = dict(row)
        d["practice_areas"] = _parse_json_list(d.pop("practice_areas_json", "[]"))
        d["tech_stack"] = _parse_json_list(d.pop("tech_stack_json", "[]"))
        d["signals"] = _parse_json_list(d.pop("signals_json", "[]"))
        return Company(**d)

    async def upsert_company(self, company: Company) -> tuple[str, bool]:
        """Добавить или обогатить компанию. Возвращает `(company_id, is_new)`."""
        domain = normalize_domain_idna(company.domain or company.website)
        ext_id = (company.external_id or "").strip()
        now = _utcnow_str()

        async with self._connect() as db:
            existing = None
            if domain:
                async with db.execute("SELECT * FROM companies WHERE domain = ?", (domain,)) as cur:
                    existing = await cur.fetchone()
            if existing is None and ext_id:
                async with db.execute("SELECT * FROM companies WHERE external_id = ?", (ext_id,)) as cur:
                    existing = await cur.fetchone()

            if existing is None:
                cid = company.id or _new_id()
                await db.execute(
                    """
                    INSERT INTO companies(
                        id, name, domain, website, legal_form, practice_areas_json,
                        description, city, address, phone, telegram, whatsapp, vk, tenchat,
                        ymaps_url, twogis_url, registry_region, external_id, source, source_url,
                        tech_stack_json, signals_json, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        company.name.strip(),
                        domain,
                        company.website.strip(),
                        company.legal_form.strip(),
                        json.dumps(company.practice_areas, ensure_ascii=False),
                        company.description.strip(),
                        company.city.strip(),
                        company.address.strip(),
                        company.phone.strip(),
                        company.telegram.strip(),
                        company.whatsapp.strip(),
                        company.vk.strip(),
                        company.tenchat.strip(),
                        company.ymaps_url.strip(),
                        company.twogis_url.strip(),
                        company.registry_region.strip(),
                        ext_id,
                        company.source.strip(),
                        company.source_url.strip(),
                        json.dumps(company.tech_stack, ensure_ascii=False),
                        json.dumps(company.signals, ensure_ascii=False),
                        company.notes.strip(),
                        now,
                        now,
                    ),
                )
                await db.commit()
                return cid, True

            cur_obj = self._company_from_row(existing)
            merged_areas = list(dict.fromkeys([*cur_obj.practice_areas, *company.practice_areas]))
            merged_stack = list(dict.fromkeys([*cur_obj.tech_stack, *company.tech_stack]))
            merged_sigs = list(dict.fromkeys([*cur_obj.signals, *company.signals]))

            await db.execute(
                """
                UPDATE companies SET
                    name = CASE WHEN name = '' THEN ? ELSE name END,
                    domain = CASE WHEN domain = '' THEN ? ELSE domain END,
                    website = CASE WHEN website = '' THEN ? ELSE website END,
                    legal_form = CASE WHEN legal_form = '' THEN ? ELSE legal_form END,
                    practice_areas_json = ?,
                    description = CASE WHEN description = '' THEN ? ELSE description END,
                    city = CASE WHEN city = '' THEN ? ELSE city END,
                    address = CASE WHEN address = '' THEN ? ELSE address END,
                    phone = CASE WHEN phone = '' THEN ? ELSE phone END,
                    telegram = CASE WHEN telegram = '' THEN ? ELSE telegram END,
                    whatsapp = CASE WHEN whatsapp = '' THEN ? ELSE whatsapp END,
                    vk = CASE WHEN vk = '' THEN ? ELSE vk END,
                    tenchat = CASE WHEN tenchat = '' THEN ? ELSE tenchat END,
                    ymaps_url = CASE WHEN ymaps_url = '' THEN ? ELSE ymaps_url END,
                    twogis_url = CASE WHEN twogis_url = '' THEN ? ELSE twogis_url END,
                    registry_region = CASE WHEN registry_region = '' THEN ? ELSE registry_region END,
                    tech_stack_json = ?,
                    signals_json = ?,
                    notes = CASE WHEN notes = '' THEN ? ELSE notes END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    company.name.strip(),
                    domain,
                    company.website.strip(),
                    company.legal_form.strip(),
                    json.dumps(merged_areas, ensure_ascii=False),
                    company.description.strip(),
                    company.city.strip(),
                    company.address.strip(),
                    company.phone.strip(),
                    company.telegram.strip(),
                    company.whatsapp.strip(),
                    company.vk.strip(),
                    company.tenchat.strip(),
                    company.ymaps_url.strip(),
                    company.twogis_url.strip(),
                    company.registry_region.strip(),
                    json.dumps(merged_stack, ensure_ascii=False),
                    json.dumps(merged_sigs, ensure_ascii=False),
                    company.notes.strip(),
                    now,
                    cur_obj.id,
                ),
            )
            await db.commit()
            return cur_obj.id, False

    async def update_company_profile_fields(
        self,
        company_id: str,
        *,
        name: str = "",
        legal_form: str = "",
        description: str = "",
        phone: str = "",
        telegram: str = "",
        whatsapp: str = "",
        vk: str = "",
        tenchat: str = "",
        practice_areas: list[str] | None = None,
        tech_stack: list[str] | None = None,
        signals: list[str] | None = None,
    ) -> None:
        existing = await self.get_company(company_id)
        if not existing:
            return
        merged_areas = list(dict.fromkeys([*existing.practice_areas, *(practice_areas or [])]))
        merged_stack = list(dict.fromkeys([*existing.tech_stack, *(tech_stack or [])]))
        merged_sigs = list(dict.fromkeys([*existing.signals, *(signals or [])]))
        now = _utcnow_str()
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE companies SET
                    name = CASE WHEN ? != '' AND (name = '' OR name LIKE '%.%') THEN ? ELSE name END,
                    legal_form = CASE WHEN legal_form = '' AND ? != '' THEN ? ELSE legal_form END,
                    description = CASE WHEN description = '' AND ? != '' THEN ? ELSE description END,
                    phone = CASE WHEN phone = '' AND ? != '' THEN ? ELSE phone END,
                    telegram = CASE WHEN telegram = '' AND ? != '' THEN ? ELSE telegram END,
                    whatsapp = CASE WHEN whatsapp = '' AND ? != '' THEN ? ELSE whatsapp END,
                    vk = CASE WHEN vk = '' AND ? != '' THEN ? ELSE vk END,
                    tenchat = CASE WHEN tenchat = '' AND ? != '' THEN ? ELSE tenchat END,
                    practice_areas_json = ?,
                    tech_stack_json = ?,
                    signals_json = ?,
                    profile_checked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name, name,
                    legal_form, legal_form,
                    description, description,
                    phone, phone,
                    telegram, telegram,
                    whatsapp, whatsapp,
                    vk, vk,
                    tenchat, tenchat,
                    json.dumps(merged_areas, ensure_ascii=False),
                    json.dumps(merged_stack, ensure_ascii=False),
                    json.dumps(merged_sigs, ensure_ascii=False),
                    now,
                    now,
                    company_id,
                ),
            )
            await db.commit()

    async def mark_profile_checked(self, company_id: str) -> None:
        now = _utcnow_str()
        async with self._connect() as db:
            await db.execute(
                "UPDATE companies SET profile_checked_at = ?, updated_at = ? WHERE id = ?",
                (now, now, company_id),
            )
            await db.commit()

    async def mark_prospects_checked(self, company_id: str) -> None:
        now = _utcnow_str()
        async with self._connect() as db:
            await db.execute(
                "UPDATE companies SET prospects_checked_at = ?, updated_at = ? WHERE id = ?",
                (now, now, company_id),
            )
            await db.commit()

    async def get_company(self, company_id: str) -> Company | None:
        async with self._connect() as db:
            async with db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)) as cur:
                row = await cur.fetchone()
                return self._company_from_row(row) if row else None

    async def get_companies(self, limit: int = 500) -> list[Company]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM companies ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                return [self._company_from_row(r) for r in await cur.fetchall()]

    async def companies_needing_profile(self, limit: int = 100, stale_days: int = 90) -> list[Company]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).strftime("%Y-%m-%d %H:%M:%S")
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT * FROM companies
                WHERE (domain != '' OR website != '')
                  AND (profile_checked_at IS NULL OR profile_checked_at < ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ) as cur:
                return [self._company_from_row(r) for r in await cur.fetchall()]

    async def companies_needing_prospects(self, limit: int = 50) -> list[Company]:
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT c.* FROM companies c
                WHERE c.prospects_checked_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM prospects p WHERE p.company_id = c.id)
                ORDER BY c.created_at ASC
                LIMIT ?
                """,
                (limit,),
            ) as cur:
                return [self._company_from_row(r) for r in await cur.fetchall()]

    # ── Prospects (Адвокаты / Управляющие партнёры / Председатели) ──

    @staticmethod
    def _prospect_from_row(row: aiosqlite.Row) -> Prospect:
        return Prospect(**dict(row))

    async def upsert_prospect(self, prospect: Prospect) -> tuple[str, bool]:
        """Добавить или обновить контакт адвоката / ЛПР с дедупликацией."""
        reg = (prospect.registry_number or "").strip()
        email = (prospect.email or "").strip().lower()
        tg = (prospect.telegram or "").strip()
        now = _utcnow_str()

        async with self._connect() as db:
            existing = None
            if reg:
                async with db.execute(
                    "SELECT * FROM prospects WHERE registry_number = ?", (reg,)
                ) as cur:
                    existing = await cur.fetchone()
            if existing is None and email:
                async with db.execute(
                    "SELECT * FROM prospects WHERE LOWER(email) = ?", (email,)
                ) as cur:
                    existing = await cur.fetchone()
            if existing is None and prospect.company_id and prospect.last_name and prospect.first_name:
                async with db.execute(
                    """
                    SELECT * FROM prospects
                    WHERE company_id = ? AND LOWER(last_name) = ? AND LOWER(first_name) = ?
                    """,
                    (prospect.company_id, prospect.last_name.strip().lower(), prospect.first_name.strip().lower()),
                ) as cur:
                    existing = await cur.fetchone()
            if existing is None and prospect.company_id and not prospect.first_name and not prospect.last_name:
                # Общий контакт практики — не более одного на компанию
                async with db.execute(
                    "SELECT * FROM prospects WHERE company_id = ? AND first_name = '' AND last_name = ''",
                    (prospect.company_id,),
                ) as cur:
                    existing = await cur.fetchone()

            if existing is None:
                pid = prospect.id or _new_id()
                await db.execute(
                    """
                    INSERT INTO prospects(
                        id, company_id, last_name, first_name, middle_name,
                        title, role_kind, registry_number, registry_status,
                        email, email_source, phone, telegram, whatsapp, tenchat, vk,
                        company, city, source, source_url, status, score,
                        personalization_notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid,
                        prospect.company_id,
                        prospect.last_name.strip(),
                        prospect.first_name.strip(),
                        prospect.middle_name.strip(),
                        prospect.title.strip(),
                        prospect.role_kind.strip(),
                        reg,
                        prospect.registry_status.strip(),
                        email,
                        prospect.email_source.strip(),
                        prospect.phone.strip(),
                        tg,
                        prospect.whatsapp.strip(),
                        prospect.tenchat.strip(),
                        prospect.vk.strip(),
                        prospect.company.strip(),
                        prospect.city.strip(),
                        prospect.source.strip(),
                        prospect.source_url.strip(),
                        prospect.status or "new",
                        int(prospect.score or 0),
                        prospect.personalization_notes.strip(),
                        now,
                        now,
                    ),
                )
                await db.commit()
                return pid, True

            cur_p = self._prospect_from_row(existing)
            await db.execute(
                """
                UPDATE prospects SET
                    middle_name = CASE WHEN middle_name = '' THEN ? ELSE middle_name END,
                    title = CASE WHEN title = '' THEN ? ELSE title END,
                    role_kind = CASE WHEN role_kind = '' THEN ? ELSE role_kind END,
                    registry_number = CASE WHEN registry_number = '' THEN ? ELSE registry_number END,
                    registry_status = CASE WHEN registry_status = '' THEN ? ELSE registry_status END,
                    email = CASE WHEN email = '' THEN ? ELSE email END,
                    email_source = CASE WHEN email_source = '' THEN ? ELSE email_source END,
                    phone = CASE WHEN phone = '' THEN ? ELSE phone END,
                    telegram = CASE WHEN telegram = '' THEN ? ELSE telegram END,
                    whatsapp = CASE WHEN whatsapp = '' THEN ? ELSE whatsapp END,
                    tenchat = CASE WHEN tenchat = '' THEN ? ELSE tenchat END,
                    vk = CASE WHEN vk = '' THEN ? ELSE vk END,
                    personalization_notes = CASE WHEN personalization_notes = '' THEN ? ELSE personalization_notes END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    prospect.middle_name.strip(),
                    prospect.title.strip(),
                    prospect.role_kind.strip(),
                    reg,
                    prospect.registry_status.strip(),
                    email,
                    prospect.email_source.strip(),
                    prospect.phone.strip(),
                    tg,
                    prospect.whatsapp.strip(),
                    prospect.tenchat.strip(),
                    prospect.vk.strip(),
                    prospect.personalization_notes.strip(),
                    now,
                    cur_p.id,
                ),
            )
            await db.commit()
            return cur_p.id, False

    async def get_prospect(self, prospect_id: str) -> Prospect | None:
        async with self._connect() as db:
            async with db.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)) as cur:
                row = await cur.fetchone()
                return self._prospect_from_row(row) if row else None

    async def get_prospects(
        self,
        *,
        status: str | None = None,
        min_score: int = 0,
        company_id: str | None = None,
        limit: int = 500,
    ) -> list[Prospect]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if min_score > 0:
            where.append("score >= ?")
            params.append(min_score)
        if company_id:
            where.append("company_id = ?")
            params.append(company_id)
        sql = "SELECT * FROM prospects"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY score DESC, created_at DESC LIMIT ?"
        params.append(limit)
        async with self._connect() as db:
            async with db.execute(sql, tuple(params)) as cur:
                return [self._prospect_from_row(r) for r in await cur.fetchall()]

    async def update_prospect_scoring(
        self,
        prospect_id: str,
        *,
        score: int,
        personalization_notes: str,
    ) -> None:
        now = _utcnow_str()
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE prospects SET
                    score = ?,
                    personalization_notes = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (max(0, min(100, int(score))), personalization_notes.strip(), now, prospect_id),
            )
            await db.commit()

    async def set_prospect_status(self, prospect_id: str, status: str) -> None:
        now = _utcnow_str()
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, prospect_id),
            )
            await db.commit()

    # ── Drafts (Черновики для ручного контакта) ──

    @staticmethod
    def _draft_from_row(row: aiosqlite.Row) -> Draft:
        d = dict(row)
        d["gate_notes"] = _parse_json_list(d.pop("gate_notes_json", "[]"))
        return Draft(**d)

    async def upsert_draft(self, draft: Draft) -> str:
        did = draft.id or _new_id()
        now = _utcnow_str()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO drafts(
                    id, prospect_id, company_id, channel, subject, body,
                    audit_summary, signal_used, status, gate_notes_json,
                    brain_provider, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prospect_id, channel) DO UPDATE SET
                    subject = excluded.subject,
                    body = excluded.body,
                    audit_summary = excluded.audit_summary,
                    signal_used = excluded.signal_used,
                    status = excluded.status,
                    gate_notes_json = excluded.gate_notes_json,
                    brain_provider = excluded.brain_provider,
                    updated_at = excluded.updated_at
                """,
                (
                    did,
                    draft.prospect_id,
                    draft.company_id,
                    draft.channel,
                    draft.subject.strip(),
                    draft.body.strip(),
                    draft.audit_summary.strip(),
                    draft.signal_used.strip(),
                    draft.status,
                    json.dumps(draft.gate_notes, ensure_ascii=False),
                    draft.brain_provider,
                    now,
                    now,
                ),
            )
            await db.commit()
            async with db.execute(
                "SELECT id FROM drafts WHERE prospect_id = ? AND channel = ?",
                (draft.prospect_id, draft.channel),
            ) as cur:
                row = await cur.fetchone()
                return row["id"] if row else did

    async def list_drafts(
        self,
        *,
        status: str | None = None,
        channel: str | None = None,
        prospect_id: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        where, params = [], []
        if status:
            where.append("d.status = ?")
            params.append(status)
        if channel:
            where.append("d.channel = ?")
            params.append(channel)
        if prospect_id:
            where.append("d.prospect_id = ?")
            params.append(prospect_id)

        sql = """
            SELECT d.*,
                   p.first_name, p.middle_name, p.last_name, p.title,
                   p.email, p.telegram AS prospect_telegram, p.phone AS prospect_phone,
                   p.score, p.registry_number,
                   c.name AS company_name, c.domain, c.website, c.city, c.legal_form,
                   c.telegram AS company_telegram
            FROM drafts d
            JOIN prospects p ON p.id = d.prospect_id
            LEFT JOIN companies c ON c.id = d.company_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.score DESC, d.updated_at DESC LIMIT ?"
        params.append(limit)
        async with self._connect() as db:
            async with db.execute(sql, tuple(params)) as cur:
                out = []
                for r in await cur.fetchall():
                    item = dict(r)
                    item["gate_notes"] = _parse_json_list(item.pop("gate_notes_json", "[]"))
                    out.append(item)
                return out

    async def set_draft_status(self, draft_id: str, status: str) -> bool:
        if status not in ("pending_review", "approved", "sent_manually", "rejected", "gate_blocked"):
            raise ValueError(f"Недопустимый статус черновика: {status}")
        now = _utcnow_str()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, draft_id),
            )
            if status == "sent_manually":
                await db.execute(
                    """
                    UPDATE prospects SET status = 'contacted_manually', updated_at = ?
                    WHERE id = (SELECT prospect_id FROM drafts WHERE id = ?)
                    """,
                    (now, draft_id),
                )
            await db.commit()
            return cur.rowcount > 0

    # ── Runs & Usage & Agent Tasks ──

    async def start_run(self, stage: str, provider: str = "", params: dict | None = None) -> str:
        run_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO runs(id, stage, status, provider, params_json, started_at)
                VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (run_id, stage, provider, json.dumps(params or {}, ensure_ascii=False), _utcnow_str()),
            )
            await db.commit()
        return run_id

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        records: int = 0,
        cost_rub: float = 0.0,
        error: str = "",
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE runs SET status = ?, records = ?, cost_rub = ?, error = ?, ended_at = ?
                WHERE id = ?
                """,
                (status, records, cost_rub, error[:500], _utcnow_str(), run_id),
            )
            await db.commit()

    async def record_usage_event(
        self,
        *,
        provider: str,
        agent: str = "",
        task: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        duration_ms: int = 0,
        is_error: bool = False,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO usage_events(
                    id, created_at, provider, agent, task, model,
                    input_tokens, output_tokens, duration_ms, is_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id(), _utcnow_str(), provider, agent, task, model,
                    input_tokens, output_tokens, duration_ms, int(is_error),
                ),
            )
            await db.commit()

    async def create_agent_task(self, kind: str, payload: dict, prompt_text: str) -> str:
        tid = _new_id()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO agent_tasks(id, kind, status, payload_json, prompt_text, created_at)
                VALUES (?, ?, 'pending', ?, ?, ?)
                """,
                (tid, kind, json.dumps(payload, ensure_ascii=False), prompt_text, _utcnow_str()),
            )
            await db.commit()
        return tid

    async def list_agent_tasks(self, status: str = "pending") -> list[dict]:
        async with self._connect() as db:
            sql = "SELECT * FROM agent_tasks"
            params: tuple = ()
            if status != "all":
                sql += " WHERE status = ?"
                params = (status,)
            sql += " ORDER BY created_at ASC"
            async with db.execute(sql, params) as cur:
                rows = []
                for r in await cur.fetchall():
                    d = dict(r)
                    try:
                        d["payload"] = json.loads(d.pop("payload_json", "{}"))
                    except ValueError:
                        d["payload"] = {}
                    rows.append(d)
                return rows

    async def complete_agent_task(self, task_id: str, response_json_str: str) -> bool:
        async with self._connect() as db:
            cur = await db.execute(
                """
                UPDATE agent_tasks
                SET status = 'completed', response_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (response_json_str, _utcnow_str(), task_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def get_state_summary(self) -> dict:
        today_prefix = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute("SELECT COUNT(*) AS cnt FROM companies") as cur:
                companies_total = (await cur.fetchone())["cnt"]
            async with db.execute(
                "SELECT COUNT(*) AS cnt FROM companies WHERE (domain != '' OR website != '') AND profile_checked_at IS NULL"
            ) as cur:
                unprofiled = (await cur.fetchone())["cnt"]
            async with db.execute("SELECT status, COUNT(*) AS cnt FROM prospects GROUP BY status") as cur:
                prospects_by_status = {r["status"]: r["cnt"] for r in await cur.fetchall()}
            async with db.execute("SELECT status, COUNT(*) AS cnt FROM drafts GROUP BY status") as cur:
                drafts_by_status = {r["status"]: r["cnt"] for r in await cur.fetchall()}
            async with db.execute("SELECT status, COUNT(*) AS cnt FROM signal_codes GROUP BY status") as cur:
                signals_by_status = {r["status"]: r["cnt"] for r in await cur.fetchall()}
            async with db.execute("SELECT COUNT(*) AS cnt FROM observations") as cur:
                observations_total = (await cur.fetchone())["cnt"]
            async with db.execute(
                "SELECT provider, COUNT(*) AS cnt FROM usage_events WHERE created_at LIKE ? AND is_error = 0 GROUP BY provider",
                (f"{today_prefix}%",),
            ) as cur:
                calls_today = {r["provider"]: r["cnt"] for r in await cur.fetchall()}
            async with db.execute("SELECT COUNT(*) AS cnt FROM agent_tasks WHERE status = 'pending'") as cur:
                pending_agent_tasks = (await cur.fetchone())["cnt"]

        return {
            "companies_total": companies_total,
            "unprofiled_companies": unprofiled,
            "prospects_by_status": prospects_by_status,
            "prospects_total": sum(prospects_by_status.values()),
            "drafts_by_status": drafts_by_status,
            "drafts_total": sum(drafts_by_status.values()),
            "signals_by_status": signals_by_status,
            "observations_total": observations_total,
            "calls_today": calls_today,
            "pending_agent_tasks": pending_agent_tasks,
        }
