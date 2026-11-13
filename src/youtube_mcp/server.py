"""One registry shared by both transports."""

from fastmcp import FastMCP

from youtube_mcp.reads import ReadError, Reads


def create_server(reads: Reads | None = None) -> FastMCP:
    """Construct the registry without credentials or external calls."""
    server = FastMCP("youtube-mcp")

    @server.tool()
    def health() -> dict[str, str]:
        """Report local process health; does not contact YouTube."""
        return {"status": "ok"}

    def configured() -> Reads:
        if reads is None:
            raise ReadError("YouTube reads are not configured.")
        return reads

    @server.tool()
    def get_channel(profile_id: str) -> dict:
        """Read exactly the explicitly selected profile's bound channel."""
        return configured().channel(profile_id)

    @server.tool()
    def get_video(profile_id: str, video_id: str) -> dict:
        """Read a video owned by the explicitly selected profile's channel."""
        return configured().require_owned_video(profile_id, video_id)

    @server.tool()
    def list_playlists(profile_id: str, page_token: str | None = None) -> dict:
        """Read a page of the bound channel's playlists."""
        return configured().playlists(profile_id, page_token)

    @server.tool()
    def list_playlist_items(
        profile_id: str, playlist_id: str, page_token: str | None = None
    ) -> dict:
        """Read a playlist page using the explicitly selected profile's grant."""
        return configured().playlist_items(profile_id, playlist_id, page_token)

    return server
