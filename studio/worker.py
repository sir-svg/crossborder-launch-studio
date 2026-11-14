import asyncio
import logging
import signal
from contextlib import suppress

from studio.main import check_settings
from studio.model import Model, Settings
from studio.store import Store
from studio.workflow import Workflow


async def serve():
    settings = Settings()
    model = Model(settings)
    check_settings(settings, model)
    store = Store(settings.database_path, settings.database_url.get_secret_value())
    try:
        await asyncio.to_thread(store.initialize, not settings.auth_enabled)
        workflow = Workflow(store, model)
        jobs = [asyncio.create_task(workflow.worker()) for _ in range(settings.worker_concurrency)]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGTERM, stop.set)
        stop_job = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([*jobs, stop_job], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in [*jobs, stop_job]:
                task.cancel()
            await asyncio.gather(*jobs, stop_job, return_exceptions=True)
    finally:
        await asyncio.to_thread(store.close)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with suppress(KeyboardInterrupt):
        asyncio.run(serve())
