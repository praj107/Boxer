"""python -m boxer_mcp — start the Boxer MCP server (stdio transport)."""
from __future__ import annotations

import logging
import sys

from boxer_mcp.tools import mcp


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
