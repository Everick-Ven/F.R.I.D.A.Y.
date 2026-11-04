"""Тесты SQLite WAL, каталога сигналов РФ, триггера observations и дедупликации .рф / ИНН."""

from __future__ import annotations

import aiosqlite
import pytest

from friday.models.company import Company
from friday.state import StateManager


@pytest.mark.asyncio
async def test_unknown_signal_code_aborted_by_sqlite_trigger(state: StateManager):
    with pytest.raises(aiosqlite.IntegrityError):
        await state.record_observation(
            signal_code="NON_EXISTENT_SIGNAL_999",
            company_id="dummy",
            require_confirmed=False,
        )


@pytest.mark.asyncio
async def test_idn_rf_and_inn_deduplication_and_cohort(state: StateManager):
    c1_id, created1 = await state.upsert_company(
        Company(
            name="ООО ПромСнаб",
            website="https://промснаб-тест.рф",
            industry="Оптовая торговля",
            inn="7701987654",
            city="Москва",
        )
    )
    assert created1 is True

    # Повторная запись с punycode того же домена .рф не создаёт дубликат
    puny = "промснаб-тест.рф".encode("idna").decode("ascii")
    c2_id, created2 = await state.upsert_company(
        Company(name="ООО ПромСнаб МСК", website=f"http://www.{puny}/about", city="Москва")
    )
    assert created2 is False
    assert c1_id == c2_id

    c3_id, _ = await state.upsert_company(
        Company(name="Завод Вектор", website="https://vektor-zavod.ru", industry="Производство", city="Казань")
    )

    await state.record_observation(signal_code="HH_HIRING_ROLE", company_id=c1_id, value_text="РОП")
    await state.record_observation(signal_code="NO_ONLINE_CAPTURE", company_id=c1_id, value_text="Нет форм")
    await state.record_observation(signal_code="HH_HIRING_ROLE", company_id=c3_id, value_text="Логист")

    both = await state.cohort(require=["HH_HIRING_ROLE", "NO_ONLINE_CAPTURE"])
    assert [c.id for c in both] == [c1_id]

    excl = await state.cohort(require=["HH_HIRING_ROLE"], exclude=["NO_ONLINE_CAPTURE"])
    assert [c.id for c in excl] == [c3_id]
