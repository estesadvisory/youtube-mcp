"""Slice 8: assign owned videos to existing playlists (Refs portfolio-ops#285)."""

from youtube_mcp.reads import ReadError, Reads


class PlaylistError(ValueError):
    """Fixed-message, secret-safe assignment failure."""


class Playlists:
    def __init__(self, reads: Reads):
        self.reads = reads

    def _membership(self, client, playlist_id: str, video_id: str) -> bool:
        parameters = dict(
            part="snippet,contentDetails", playlistId=playlist_id, videoId=video_id, maxResults=50
        )
        seen = set()
        for _ in range(100):
            response = self.reads._list(client, "playlistItems", **parameters)
            for item in response["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("snippet"), dict):
                    raise ReadError("Invalid playlist membership response.")
                snippet = item["snippet"]
                resource = snippet.get("resourceId")
                if not isinstance(resource, dict):
                    raise ReadError("Invalid playlist membership response.")
                if (
                    snippet.get("playlistId") == playlist_id
                    and resource.get("kind") == "youtube#video"
                    and resource.get("videoId") == video_id
                ):
                    return True
            token = response.get("nextPageToken")
            if not token:
                return False
            if not isinstance(token, str) or token in seen:
                raise ReadError("Invalid playlist pagination.")
            seen.add(token)
            parameters["pageToken"] = token
        raise ReadError("Playlist pagination limit reached.")

    def assign(self, profile_id: str, video_id: str, playlist_id: str) -> dict:
        self.reads._profiles.resolve(profile_id)
        if any(
            not isinstance(value, str) or not value.strip() for value in (video_id, playlist_id)
        ):
            raise PlaylistError("Explicit video and playlist ids are required.")
        profile, client, _ = self.reads._bound(profile_id)
        self.reads._owned_video(profile, client, video_id)
        response = self.reads._list(client, "playlists", part="snippet", id=playlist_id)
        items = response["items"]
        if (
            len(items) != 1
            or not isinstance(items[0], dict)
            or items[0].get("id") != playlist_id
            or not isinstance(items[0].get("snippet"), dict)
            or items[0]["snippet"].get("channelId") != profile.channel_id
            or response.get("nextPageToken")
        ):
            raise PlaylistError("Playlist is unavailable or not owned by the bound channel.")
        result = {"video_id": video_id, "playlist_id": playlist_id}
        if self._membership(client, playlist_id, video_id):
            return {**result, "state": "already_present"}
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }
        try:
            client.insert_playlist_item(body, part="snippet")
        except Exception:
            # Completion may have reached Google. Never blindly repeat this write.
            return {**result, "state": "ambiguous"}
        try:
            matches = self._membership(client, playlist_id, video_id)
        except ReadError:
            return {**result, "state": "verification_pending"}
        return {**result, "state": "succeeded" if matches else "mismatch"}
