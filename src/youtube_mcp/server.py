"""One registry shared by both transports."""

from fastmcp import FastMCP


def create_server() -> FastMCP:
    """Construct the registry without credentials or external calls."""
    server = FastMCP("youtube-mcp")

    @server.tool()
    def health() -> dict[str, str]:
        """Report local process health; does not contact YouTube."""
        return {"status": "ok"}

    return server
