"""Общие фикстуры тестов F.R.I.D.A.Y. (все данные строго вымышленные: *.example)."""

import pytest_asyncio

from friday.signals import seed_signal_catalog
from friday.state import StateManager


@pytest_asyncio.fixture
async def state_db(tmp_path):
    db_file = tmp_path / "test_friday.db"
    st = StateManager(db_path=db_file)
    await st.init_db()
    await seed_signal_catalog(st, auto_confirm_free=True)
    return st
