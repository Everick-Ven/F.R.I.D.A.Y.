"""Тесты SQLite состояния, управляемого словаря сигналов, триггера наблюдений и когорт."""

import aiosqlite
import pytest

from friday.models import Company
from friday.signals import seed_signal_catalog
from friday.state import StateManager


@pytest.mark.asyncio
async def test_signal_vocabulary_gate_and_trigger(tmp_path):
    st = StateManager(db_path=tmp_path / "gov.db")
    await st.init_db()
    count = await seed_signal_catalog(st, auto_confirm_free=False)
    assert count >= 20

    cid, is_new = await st.upsert_company(
        Company(name="КА «Север»", domain="sever-law.example", city="Москва")
    )
    assert is_new is True

    # Пока сигнал в статусе proposed — он НЕ записывается
    obs_none = await st.record_observation(
        signal_code="NO_YANDEX_METRIKA",
        company_id=cid,
        collector="profile",
        value_num=1.0,
    )
    assert obs_none is None

    # После подтверждения человеком — записывается
    await st.set_signal_status("NO_YANDEX_METRIKA", "confirmed")
    obs_ok = await st.record_observation(
        signal_code="NO_YANDEX_METRIKA",
        company_id=cid,
        collector="profile",
        value_num=1.0,
        evidence_url="https://sever-law.example",
    )
    assert obs_ok is not None

    # Попытка вставить сигнал вне словаря отклоняется SQLite-триггером
    with pytest.raises(aiosqlite.IntegrityError):
        await st.record_observation(
            signal_code="INVENTED_JUNK_SIGNAL",
            company_id=cid,
        )


@pytest.mark.asyncio
async def test_idn_rf_dedup_and_cohort_intersection(state_db: StateManager):
    # Кириллический домен .рф и его punycode должны склеиваться в одну практику
    cid1, new1 = await state_db.upsert_company(
        Company(name="АБ «Правоведъ»", website="https://адвокат-тест.рф", city="Казань")
    )
    cid2, new2 = await state_db.upsert_company(
        Company(name="АБ «Правоведъ»", domain="www.xn----7sbahjj6a5a5ahbc.xn--p1ai", phone="+7 (843) 000-00-01")
    )
    assert new1 is True
    assert new2 is False
    assert cid1 == cid2

    cid_other, _ = await state_db.upsert_company(
        Company(name="КА «Вектор»", domain="vektor-law.example", city="Казань")
    )

    await state_db.record_observation(signal_code="NO_YANDEX_METRIKA", company_id=cid1, value_num=1.0)
    await state_db.record_observation(signal_code="MISSING_PRACTICE_PAGES", company_id=cid1, value_num=1.0)

    await state_db.record_observation(signal_code="NO_YANDEX_METRIKA", company_id=cid_other, value_num=1.0)
    await state_db.record_observation(signal_code="RUNNING_YANDEX_DIRECT", company_id=cid_other, value_text="Calltouch")

    cohort_a = await state_db.cohort(
        require=["NO_YANDEX_METRIKA", "MISSING_PRACTICE_PAGES"],
        exclude=["RUNNING_YANDEX_DIRECT"],
    )
    assert [c.id for c in cohort_a] == [cid1]
