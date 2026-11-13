"""One registry shared by both transports."""

from fastmcp import FastMCP

from youtube_mcp.metadata import Metadata, MetadataError
from youtube_mcp.reads import ReadError, Reads
from youtube_mcp.thumbnails import ThumbnailError, Thumbnails
from youtube_mcp.uploads import UploadError, Uploads


def create_server(
    reads: Reads | None = None,
    uploads: Uploads | None = None,
    thumbnails: Thumbnails | None = None,
    metadata: Metadata | None = None,
) -> FastMCP:
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

    @server.tool()
    def upload_video(
        profile_id: str,
        media_path: str,
        snippet: dict,
        privacy_status: str,
        self_declared_made_for_kids: bool,
        contains_synthetic_media: bool,
        publish_at: str | None = None,
    ) -> dict:
        """Start a resumable upload job for an explicit profile and allowed local file.

        Both audience/disclosure booleans and privacy are required. A schedule
        requires private privacy. Call resume_upload to advance the job.
        """
        if uploads is None:
            raise UploadError("YouTube uploads are not configured.")
        job = uploads.start(
            profile_id,
            media_path,
            snippet,
            privacy_status,
            self_declared_made_for_kids,
            contains_synthetic_media,
            publish_at,
        )

        return uploads.advance(profile_id, job["job_id"])

    @server.tool()
    def resume_upload(profile_id: str, job_id: str) -> dict:
        """Advance a recorded upload by one chunk or retry pending verification.

        Returns only job id, state and video id; resumable URLs are secrets.
        An ambiguous job without a recorded session requires operator recovery.
        """
        if uploads is None:
            raise UploadError("YouTube uploads are not configured.")
        return uploads.advance(profile_id, job_id)

    @server.tool()
    def set_thumbnail(profile_id: str, video_id: str, media_path: str) -> dict:
        """Set a local JPEG/PNG on an owned video; verify a returned thumbnail URL.

        Success confirms URL metadata only, never byte-for-byte image identity.
        """
        if thumbnails is None:
            raise ThumbnailError("YouTube thumbnails are not configured.")
        return thumbnails.set(profile_id, video_id, media_path)

    @server.tool()
    def update_video(profile_id: str, video_id: str, snippet: dict, status: dict) -> dict:
        """Update an owned video; status.selfDeclaredMadeForKids is required.

        Only supplied fields change. publishAt requires explicit private privacy.
        Read-back returns succeeded, mismatch, or verification_pending.
        """
        if metadata is None:
            raise MetadataError("YouTube metadata updates are not configured.")
        return metadata.update(profile_id, video_id, snippet, status)

    return server
