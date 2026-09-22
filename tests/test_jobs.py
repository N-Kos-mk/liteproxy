import asyncio

from liteproxy.browser import RenderError, Viewport
from liteproxy.jobs import RenderJobs, RenderKey

KEY = RenderKey("https://8.8.8.8/", Viewport(390, 844, 3.0, False), "UA")


class SlowRenderer:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.release = asyncio.Event()

    async def start(self):
        pass

    async def stop(self):
        pass

    async def render(self, url, viewport, user_agent, on_stage=None):
        self.calls += 1
        on_stage("fetching")
        await self.release.wait()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_same_request_while_rendering_reuses_job():
    async def run():
        renderer = SlowRenderer(["html"])
        jobs = RenderJobs(renderer)
        first = jobs.start(KEY)
        await asyncio.sleep(0)
        second = jobs.start(KEY)  # 描画中の再読み込み
        assert first is second
        renderer.release.set()
        await first.task
        assert jobs.cached(KEY) == "html"
        assert jobs.start(KEY) is first  # 有効な結果があれば描画し直さない
        assert renderer.calls == 1

    asyncio.run(run())


def test_failed_job_is_not_reused():
    async def run():
        renderer = SlowRenderer([RenderError("失敗"), "html"])
        renderer.release.set()
        jobs = RenderJobs(renderer)
        failed = jobs.start(KEY)
        await failed.task
        assert failed.error == "失敗" and jobs.cached(KEY) is None
        retry = jobs.start(KEY)
        assert retry is not failed
        await retry.task
        assert jobs.cached(KEY) == "html"

    asyncio.run(run())


def test_wait_change_wakes_on_stage_and_times_out():
    async def run():
        renderer = SlowRenderer(["html"])
        jobs = RenderJobs(renderer, on_complete=lambda job, result: 1234)
        job = jobs.start(KEY)
        seen = await job.wait_change(-1, 1.0)  # 開始時点の版はすぐ返る
        seen = await job.wait_change(seen, 1.0)  # "fetching" への変化
        assert job.stage == "fetching"
        assert await job.wait_change(seen, 0.05) == seen  # 変化がなければ時間切れで同じ版
        renderer.release.set()
        await job.wait_change(seen, 1.0)
        assert job.result == "html" and job.sent_bytes == 1234

    asyncio.run(run())


def test_old_results_expire():
    async def run():
        renderer = SlowRenderer(["html"])
        renderer.release.set()
        jobs = RenderJobs(renderer, ttl=0.0)
        job = jobs.start(KEY)
        await job.task
        assert jobs.cached(KEY) is None and jobs.get(job.id) is None

    asyncio.run(run())
