"""python -m boxerd — start the Boxer daemon."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from boxerd.daemon import BoxerDaemon


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    daemon = BoxerDaemon()

    async def _run() -> None:
        await daemon.start()
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            stop_event.set()

        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        loop.add_signal_handler(signal.SIGINT, _signal_handler)

        await stop_event.wait()
        await daemon.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
