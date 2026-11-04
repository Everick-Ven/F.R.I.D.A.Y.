"""Общие фикстуры pytest для F.R.I.D.A.Y. v0.2."""

from __future__ import annotations

from pathlib import Path
import pytest_asyncio

from friday.pipeline import bootstrap_state
from friday.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path: Path) -> StateManager:
    db_file = tmp_path / "test_friday.db"
    sm = StateManager(db_path=db_file)
    await bootstrap_state(sm, auto_confirm_core=True)
    return sm
