"""Трекер фоновых операций с пошаговым прогрессом в реальном времени."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class JobStep:
    label: str
    state: str = "pending"  # pending | active | done | error


@dataclass
class JobRecord:
    id: str
    kind: str
    title: str
    status: str = "running"  # running | completed | failed
    percent: int = 5
    stage: str = ""
    steps: list[JobStep] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def set_step(self, index: int, *, percent: int, detail: str = "") -> None:
        self.percent = max(0, min(100, percent))
        for i, st in enumerate(self.steps):
            if i < index:
                st.state = "done"
            elif i == index:
                st.state = "active"
                self.stage = detail or st.label
            else:
                st.state = "pending"

    def complete(self, result: dict, summary_stage: str = "Завершено") -> None:
        self.status = "completed"
        self.percent = 100
        self.stage = summary_stage
        self.result = result
        self.finished_at = time.time()
        for st in self.steps:
            st.state = "done"

    def fail(self, message: str) -> None:
        raw = str(message or "").strip()
        if any(tok in raw.lower() for tok in ("_ssl.c", "eof", "connecterror", "timed out")):
            clean_msg = "Внешний источник временно недоступен по сети. Повторите запрос или используйте импорт файла."
        else:
            clean_msg = raw or "Не удалось завершить операцию"
        self.status = "failed"
        self.error = clean_msg
        self.stage = clean_msg
        self.finished_at = time.time()
        for st in self.steps:
            if st.state == "active":
                st.state = "error"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "percent": self.percent,
            "stage": self.stage,
            "steps": [{"label": s.label, "state": s.state} for s in self.steps],
            "result": self.result,
            "error": self.error,
            "elapsed_sec": round((self.finished_at or time.time()) - self.started_at, 1),
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(
        self,
        *,
        kind: str,
        title: str,
        step_labels: list[str],
        runner: Callable[[JobRecord], Awaitable[dict]],
    ) -> JobRecord:
        jid = uuid.uuid4().hex[:10]
        steps = [JobStep(label=lbl) for lbl in step_labels]
        if steps:
            steps[0].state = "active"
        job = JobRecord(
            id=jid,
            kind=kind,
            title=title,
            stage=steps[0].label if steps else title,
            steps=steps,
        )
        self._jobs[jid] = job

        # Очищаем старые завершённые задачи, чтобы не копить больше 40 записей
        if len(self._jobs) > 40:
            done_ids = [k for k, v in self._jobs.items() if v.status != "running"]
            for old_id in done_ids[:-20]:
                self._jobs.pop(old_id, None)

        async def _wrap() -> None:
            try:
                res = await runner(job)
                if job.status == "running":
                    job.complete(res)
            except Exception as exc:
                job.fail(str(exc))

        task = asyncio.create_task(_wrap())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def active_jobs(self) -> list[dict]:
        return [j.as_dict() for j in self._jobs.values() if j.status == "running"]
