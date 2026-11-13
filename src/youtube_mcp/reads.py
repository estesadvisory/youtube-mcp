"""Offline-injectable read boundary and channel guards (Refs portfolio-ops#285)."""

from typing import Any, Protocol

from youtube_mcp.profiles import Credential, CredentialProvider, Profile, ProfileStore


class ReadError(ValueError):
    """Fixed-message failure safe to expose to a caller."""


class YouTubeClient(Protocol):
    """Adapter port for the Data API reads and resumable uploads; no live adapter."""

    def list(self, resource: str, **parameters: Any) -> dict[str, Any]: ...

    def insert_caption(self, body: dict[str, Any], media: bytes, *, part: str) -> dict[str, Any]:
        """Insert SRT once; raise captions.CaptionExists for 409 captionExists."""
        ...

    def insert_playlist_item(self, body: dict[str, Any], *, part: str) -> dict[str, Any]:
        """Insert one playlist membership; no live adapter in core."""
        ...

    def set_thumbnail(self, video_id: str, media: bytes, mime_type: str) -> dict[str, Any]:
        """Set a thumbnail once; return the thumbnails.set response."""
        ...

    def update(self, body: dict[str, Any], *, part: str) -> dict[str, Any]:
        """Replace selected video parts; no live adapter in core."""
        ...

    def begin_insert(self, body: dict[str, Any], size: int, job_id: str) -> str:
        """Start resumable videos.insert; return a secret session URL."""
        ...

    def probe_insert(self, session: str) -> dict[str, Any]:
        """Read recorded session progress/completion before any retry.

        Return offset and optional video_id. Never log the session URL.
        """
        ...

    def upload_chunk(self, session: str, offset: int, data: bytes, total: int) -> dict[str, Any]:
        """Send one chunk; return offset and optional completed video_id."""
        ...


class ClientFactory(Protocol):
    def __call__(self, credential: Credential) -> YouTubeClient: ...


class Reads:
    def __init__(
        self, profiles: ProfileStore, credentials: CredentialProvider, clients: ClientFactory
    ) -> None:
        self._profiles = profiles
        self._credentials = credentials
        self._clients = clients

    @staticmethod
    def _list(client: YouTubeClient, resource: str, **parameters: Any) -> dict[str, Any]:
        try:
            response = client.list(resource, **parameters)
        except Exception:
            raise ReadError("YouTube read failed.") from None
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise ReadError("Invalid YouTube list response.")
        return response

    def _bound(self, profile_id: str) -> tuple[Profile, YouTubeClient, dict[str, Any]]:
        profile = self._profiles.resolve(profile_id)
        credential = self._credentials.resolve(profile.credential_ref)
        try:
            client = self._clients(credential)
        except Exception:
            raise ReadError("YouTube client unavailable.") from None
        channel = self._list(client, "channels", part="snippet,contentDetails", mine=True)
        items = channel["items"]
        page_info = channel.get("pageInfo", {})
        if (
            len(items) != 1
            or not isinstance(items[0], dict)
            or items[0].get("id") != profile.channel_id
            or channel.get("nextPageToken")
            or not isinstance(page_info, dict)
            or page_info.get("totalResults", 1) != 1
        ):
            raise ReadError("Grant must resolve to exactly the bound channel.")
        return profile, client, channel

    def channel(self, profile_id: str) -> dict[str, Any]:
        return self._bound(profile_id)[2]

    @staticmethod
    def _owned_video(profile: Profile, client: YouTubeClient, video_id: str) -> dict[str, Any]:
        response = Reads._list(client, "videos", part="snippet,status,contentDetails", id=video_id)
        items = response["items"]
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ReadError("Video is unavailable or not owned by the bound channel.")
        video = items[0]
        snippet = video.get("snippet")
        if (
            video.get("id") != video_id
            or not isinstance(snippet, dict)
            or snippet.get("channelId") != profile.channel_id
        ):
            raise ReadError("Video is unavailable or not owned by the bound channel.")
        return response

    def require_owned_video(self, profile_id: str, video_id: str) -> dict[str, Any]:
        """Recheck grant and snippet.channelId before any future video mutation."""
        profile, client, _ = self._bound(profile_id)
        return self._owned_video(profile, client, video_id)

    def playlists(self, profile_id: str, page_token: str | None = None) -> dict[str, Any]:
        profile, client, _ = self._bound(profile_id)
        parameters = dict(
            part="snippet,contentDetails", channelId=profile.channel_id, maxResults=50
        )
        if page_token is not None:
            parameters["pageToken"] = page_token
        return self._list(client, "playlists", **parameters)

    def playlist_items(
        self, profile_id: str, playlist_id: str, page_token: str | None = None
    ) -> dict[str, Any]:
        _, client, _ = self._bound(profile_id)
        parameters = dict(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50)
        if page_token is not None:
            parameters["pageToken"] = page_token
        return self._list(client, "playlistItems", **parameters)
