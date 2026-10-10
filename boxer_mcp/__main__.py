"""python -m boxer_mcp — start the Boxer MCP server (stdio transport)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from boxer_mcp.profiles import profile_summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Start the Boxer MCP stdio server.")
    parser.add_argument(
        "--profile",
        default=os.environ.get("BOXER_MCP_PROFILE", "codex"),
        help="MCP operating profile: codex, claude, qwen, deepseek, kimi, oss, readonly (default: %(default)s)",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="Print available MCP profiles as JSON and exit.",
    )
    args = parser.parse_args(argv)

    if args.list_profiles:
        print(json.dumps(profile_summary(), indent=2))
        return

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    from boxer_mcp.tools import build_mcp

    build_mcp(args.profile).run(transport="stdio")


if __name__ == "__main__":
    main()
