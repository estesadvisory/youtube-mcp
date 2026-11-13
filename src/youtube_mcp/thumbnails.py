"""Plan Slice 6: confined, fake-injectable thumbnail writes (Refs portfolio-ops#285)."""

from pathlib import Path
from typing import Any

from youtube_mcp.reads import Reads
from youtube_mcp.uploads import Uploads


class ThumbnailError(ValueError):
    """Fixed-message thumbnail failure, safe to expose."""


class Thumbnails:
    # Share Slice 4's descriptor-based, symlink-race-resistant media confinement.
    _media = Uploads._media

    def __init__(self, reads: Reads, media_roots: dict[str, Path]):
        self.reads = reads
        self.roots = {key: Path(value).resolve(strict=True) for key, value in media_roots.items()}

    @staticmethod
    def _urls(value: Any) -> list[str]:
        if not isinstance(value, dict):
            return []
        return [
            item["url"]
            for item in value.values()
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]
        ]

    def set(self, profile_id: str, video_id: str, media_path: str) -> dict:
        self.reads._profiles.resolve(profile_id)
        if not isinstance(video_id, str) or not video_id.strip():
            raise ThumbnailError("Explicit video id is required.")
        with self._media(profile_id, media_path) as stream:
            # YouTube accepts thumbnails up to 2 MB; bound local memory consumption.
            media = stream.read(2 * 1024 * 1024 + 1)
        if not media or len(media) > 2 * 1024 * 1024:
            raise ThumbnailError("Thumbnail must contain at most 2 MB.")
        if media.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif media.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            raise ThumbnailError("Thumbnail must be JPEG or PNG media.")
        profile, client, _ = self.reads._bound(profile_id)
        self.reads._owned_video(profile, client, video_id)
        try:
            response = client.set_thumbnail(video_id, media, mime)
        except Exception:
            # A lost response may still have mutated Google; never retry here.
            return {"state": "ambiguous", "video_id": video_id}
        urls = []
        if isinstance(response, dict) and isinstance(response.get("items"), list):
            for item in response["items"]:
                urls.extend(self._urls(item))
        if not urls:
            try:
                observed = self.reads._owned_video(profile, client, video_id)
                urls = self._urls(observed["items"][0]["snippet"].get("thumbnails"))
            except Exception:
                pass
        return {
            "state": "succeeded" if urls else "verification_pending",
            "video_id": video_id,
            "thumbnail_urls": urls,
        }
