from __future__ import annotations

import logging
import sys
from contextlib import redirect_stdout
from pathlib import Path

import anyio
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server

from app.services.git_miner import GitMiner
from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRFetcher
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex
from mcp_server.tools import ToolServices, register_tools

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


def _init_services() -> ToolServices:
    with redirect_stdout(sys.stderr):
        scar_index = ScarIndex()
        nia_client = NiaClient()
        reviewer = Reviewer(scar_index, nia_client, AsyncAnthropic())
        pr_fetcher = PRFetcher()
        git_miner = GitMiner()
    return ToolServices(
        scar_index=scar_index,
        nia_client=nia_client,
        reviewer=reviewer,
        pr_fetcher=pr_fetcher,
        git_miner=git_miner,
    )


logger.info("Initializing ScarTissue MCP services")
SERVICES = _init_services()
SERVER = Server("scartissue")
register_tools(SERVER, SERVICES)


async def _run() -> None:
    logger.info("Starting ScarTissue MCP stdio server")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await SERVER.run(
                read_stream,
                write_stream,
                SERVER.create_initialization_options(
                    notification_options=NotificationOptions(),
                ),
            )
    finally:
        await SERVICES.nia_client.aclose()
        logger.info("ScarTissue MCP stdio server stopped")


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
