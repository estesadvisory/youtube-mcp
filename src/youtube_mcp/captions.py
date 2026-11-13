"""Plan Slice 7: local SRT insert and list verification (Refs portfolio-ops#285)."""

from pathlib import Path

from youtube_mcp.reads import Reads
from youtube_mcp.uploads import Uploads


class CaptionError(ValueError):
    """Fixed-message caption input failure."""


class CaptionExists(Exception):
    """Client port outcome for YouTube 409 captionExists; never repaired."""


class Captions:
    _media = Uploads._media

    def __init__(self, reads: Reads, media_roots: dict[str, Path]):
        self.reads = reads
        self.roots = {key: Path(value).resolve(strict=True) for key, value in media_roots.items()}

    def insert(
        self,
        profile_id: str,
        video_id: str,
        language: str,
        name: str,
        is_draft: bool,
        media_path: str,
    ) -> dict:
        self.reads._profiles.resolve(profile_id)
        if any(not isinstance(v, str) or not v.strip() for v in (video_id, language, name)):
            raise CaptionError("Explicit video id, language and name are required.")
        if type(is_draft) is not bool:
            raise CaptionError("Explicit isDraft boolean is required.")
        with self._media(profile_id, media_path) as stream:
            media = stream.read(10 * 1024 * 1024 + 1)
        if not media or len(media) > 10 * 1024 * 1024:
            raise CaptionError("SRT media must contain at most 10 MB.")
        try:
            media.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CaptionError("SRT media must be UTF-8 text.") from None
        profile, client, _ = self.reads._bound(profile_id)
        self.reads._owned_video(profile, client, video_id)
        snippet = dict(videoId=video_id, language=language, name=name, isDraft=is_draft)
        result = {"video_id": video_id}
        try:
            response = client.insert_caption({"snippet": snippet}, media, part="snippet")
        except CaptionExists:
            return {**result, "state": "captionExists", "status": 409}
        except Exception:
            return {**result, "state": "ambiguous"}
        caption_id = response.get("id") if isinstance(response, dict) else None
        # No stable id means we cannot distinguish an existing track from this insert.
        if isinstance(caption_id, str) and caption_id:
            result["caption_id"] = caption_id
        try:
            listing = self.reads._list(client, "captions", part="snippet", videoId=video_id)
        except Exception:
            return {**result, "state": "verification_pending"}
        if "caption_id" not in result:
            return {**result, "state": "verification_pending"}
        for track in listing["items"]:
            if isinstance(track, dict) and track.get("id") == caption_id:
                observed = track.get("snippet")
                matches = isinstance(observed, dict) and all(
                    key in observed
                    and type(observed[key]) is type(value)
                    and observed[key] == value
                    for key, value in snippet.items()
                )
                return {**result, "state": "succeeded" if matches else "mismatch"}
        return {**result, "state": "verification_pending"}
