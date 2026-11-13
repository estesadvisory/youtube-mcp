"""Select a transport without changing the tool registry."""

import argparse
import ipaddress
import logging
import sys
from copy import deepcopy

from uvicorn.config import LOGGING_CONFIG

from youtube_mcp.server import create_server


def loopback_address(value: str) -> str:
    """Accept only literal loopback IPs; never resolve attacker-controlled DNS."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("host must be a literal loopback IP address") from exc
    if not address.is_loopback or getattr(address, "ipv4_mapped", None):
        raise argparse.ArgumentTypeError(
            "non-loopback HTTP binding requires a caller-auth policy; "
            "Slice 1 has no caller-auth policy and refuses this bind"
        )
    return str(address)


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube MCP transport shell")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", type=loopback_address, default="127.0.0.1")
    parser.add_argument("--port", type=int, choices=range(1, 65536), default=8000, metavar="PORT")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    server = create_server()
    if args.transport == "stdio":
        server.run(transport="stdio", show_banner=False)
    else:
        log_config = deepcopy(LOGGING_CONFIG)
        log_config["handlers"]["access"]["stream"] = "ext://sys.stderr"
        server.run(
            transport="http",
            host=args.host,
            port=args.port,
            uvicorn_config={"log_config": log_config},
            show_banner=False,
        )


if __name__ == "__main__":
    main()
