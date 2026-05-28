"""boxer-notifier — user systemd service that polls events and sends desktop notifications."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timezone

from boxer.config import get_config
from boxer_mcp.ipc_client import make_call

logger = logging.getLogger(__name__)


async def _notify(message: str) -> None:
    try:
        subprocess.run(
            ["notify-send", "--app-name=Boxer", "--urgency=normal", "Boxer", message],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        logger.warning("notify-send not found; install libnotify-bin")


async def run() -> None:
    cfg = get_config()
    since = datetime.now(timezone.utc).isoformat()
    warn_count = 0

    while True:
        try:
            events = await make_call(
                cfg.notify_socket_path,
                "event.poll",
                {"since": since, "limit": 50},
            )
            for evt in reversed(events):
                if evt["level"] in ("WARN", "ERROR"):
                    await _notify(evt["message"])
                    warn_count += 1
                since = evt["created_at"]
        except Exception as exc:
            logger.debug("Poll failed: %s", exc)

        await asyncio.sleep(60)


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
