"""描画ジョブの管理。

描画には数秒かかるため、/p への要求ではまず読み込み中画面を返し、描画はジョブとして
裏で進める。ジョブは段階が変わるたびに待機中の読み込み中画面へ知らせ、結果は短時間
保持して /v での表示や「戻る」・再読み込みに使う。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .browser import STAGE_QUEUED, NonHtml, RenderError, Snapshot, StageCallback, Viewport

log = logging.getLogger(__name__)


class Renderer(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def render(
        self, url: str, viewport: Viewport, user_agent: str | None, on_stage: StageCallback | None = None
    ) -> Snapshot | NonHtml: ...


@dataclass(frozen=True)
class RenderKey:
    """同じ結果を使い回してよい描画条件。"""

    url: str
    viewport: Viewport
    user_agent: str


class Job:
    def __init__(self, key: RenderKey) -> None:
        self.id = secrets.token_urlsafe(9)
        self.key = key
        self.stage = STAGE_QUEUED
        self.result: Snapshot | NonHtml | None = None
        self.error: str | None = None
        self.sent_bytes: int | None = None  # スマホへの送信量の目安（gzip 後）
        self.started = time.monotonic()
        self.finished: float | None = None
        self.task: asyncio.Task | None = None
        self._version = 0
        self._waiters: list[asyncio.Future] = []

    @property
    def done(self) -> bool:
        return self.finished is not None

    def elapsed_ms(self) -> int:
        return round(((self.finished or time.monotonic()) - self.started) * 1000)

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self._changed()

    def finish(self, result: Snapshot | NonHtml, sent_bytes: int | None) -> None:
        self.result = result
        self.sent_bytes = sent_bytes
        self.finished = time.monotonic()
        self._changed()

    def fail(self, message: str) -> None:
        self.error = message
        self.finished = time.monotonic()
        self._changed()

    async def wait_change(self, seen: int, timeout: float) -> int:
        """seen 以降に状態が変わるか timeout 秒経つまで待ち、現在の版を返す。"""
        if self._version == seen:
            future = asyncio.get_running_loop().create_future()
            self._waiters.append(future)
            await asyncio.wait({future}, timeout=timeout)
        return self._version

    def _changed(self) -> None:
        self._version += 1
        for future in self._waiters:
            if not future.done():
                future.set_result(None)
        self._waiters.clear()


class RenderJobs:
    def __init__(
        self,
        renderer: Renderer,
        *,
        ttl: float = 300.0,
        max_finished: int = 30,
        on_complete: Callable[[Job, Snapshot | NonHtml], int | None] | None = None,
    ) -> None:
        """on_complete は完了時に呼ばれ、スマホへの送信量の目安（バイト）を返す。"""
        self._renderer = renderer
        self._ttl = ttl
        self._max_finished = max_finished
        self._on_complete = on_complete
        self._jobs: dict[str, Job] = {}
        self._by_key: dict[RenderKey, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def cached(self, key: RenderKey) -> Snapshot | NonHtml | None:
        job = self._by_key.get(key)
        if job is not None and job.result is not None and self._fresh(job):
            return job.result
        return None

    def start(self, key: RenderKey) -> Job:
        """同じ条件の描画が進行中、または有効な結果があればそのジョブを返し、なければ新たに始める。"""
        current = self._by_key.get(key)
        if current is not None and current.error is None and (not current.done or self._fresh(current)):
            return current
        job = Job(key)
        self._jobs[job.id] = job
        self._by_key[key] = job
        job.task = asyncio.create_task(self._run(job))
        return job

    async def aclose(self) -> None:
        tasks = [job.task for job in self._jobs.values() if job.task is not None and not job.done]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job: Job) -> None:
        key = job.key
        try:
            result = await self._renderer.render(key.url, key.viewport, key.user_agent, on_stage=job.set_stage)
        except RenderError as e:
            job.fail(str(e))
        except asyncio.CancelledError:
            job.fail("サーバーが停止しました。")
            raise
        except Exception:
            log.exception("描画中に予期しないエラー: %s", key.url)
            job.fail("予期しないエラーが発生しました。")
        else:
            sent = self._on_complete(job, result) if self._on_complete else None
            job.finish(result, sent)
        finally:
            self._prune()

    def _fresh(self, job: Job) -> bool:
        return job.finished is not None and time.monotonic() - job.finished < self._ttl

    def _prune(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j.done), key=lambda j: j.finished or 0)
        excess = len(finished) - self._max_finished
        for i, job in enumerate(finished):
            if i < excess or not self._fresh(job):
                del self._jobs[job.id]
                if self._by_key.get(job.key) is job:
                    del self._by_key[job.key]
