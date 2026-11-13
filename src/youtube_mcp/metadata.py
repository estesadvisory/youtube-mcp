"""Guarded metadata updates on the injected port (Refs portfolio-ops#285)."""

from copy import deepcopy

from youtube_mcp.reads import ReadError, Reads

SNIPPET = {"title", "description", "tags", "categoryId", "defaultLanguage"}
STATUS = {"privacyStatus", "publishAt", "selfDeclaredMadeForKids", "containsSyntheticMedia"}
# These writable settings must survive status-part replacement, too.
PRESERVE_STATUS = STATUS | {"embeddable", "license", "publicStatsViewable"}


class MetadataError(ValueError):
    """Fixed-message, secret-safe update failure."""


class Metadata:
    def __init__(self, reads: Reads):
        self.reads = reads

    def update(self, profile_id: str, video_id: str, snippet: dict, status: dict) -> dict:
        if (
            not isinstance(snippet, dict)
            or not isinstance(status, dict)
            or not snippet.keys() <= SNIPPET
            or not status.keys() <= STATUS
            or type(status.get("selfDeclaredMadeForKids")) is not bool
        ):
            raise MetadataError("Allowed fields and explicit selfDeclaredMadeForKids are required.")
        if any(not isinstance(value, str) for key, value in snippet.items() if key != "tags") or (
            "tags" in snippet
            and (
                not isinstance(snippet["tags"], list)
                or any(not isinstance(tag, str) for tag in snippet["tags"])
            )
        ):
            raise MetadataError("Invalid snippet field type.")
        if (
            "containsSyntheticMedia" in status
            and type(status["containsSyntheticMedia"]) is not bool
        ):
            raise MetadataError("containsSyntheticMedia must be a boolean.")
        if "privacyStatus" in status and status["privacyStatus"] not in (
            "private",
            "public",
            "unlisted",
        ):
            raise MetadataError("Invalid privacyStatus.")
        if "publishAt" in status and (
            status.get("privacyStatus") != "private"
            or not isinstance(status["publishAt"], str)
            or not status["publishAt"]
        ):
            raise MetadataError("publishAt requires explicit private privacyStatus.")
        requested = deepcopy({"snippet": snippet, "status": status})
        profile, client, _ = self.reads._bound(profile_id)
        current = self.reads._owned_video(profile, client, video_id)["items"][0]
        if not isinstance(current.get("status"), dict):
            raise MetadataError("Current status is unavailable; update refused.")
        if "publishAt" in status and current["status"].get("privacyStatus") != "private":
            raise MetadataError("Only a currently private video can be scheduled.")
        body = {"id": video_id}
        for section, allowed in (("snippet", SNIPPET), ("status", PRESERVE_STATUS)):
            if not requested[section]:
                continue
            # videos.update replaces parts, not just supplied properties. Preserve
            # read values, never invent defaults or send read-only properties.
            body[section] = {
                key: deepcopy(value) for key, value in current[section].items() if key in allowed
            }
            body[section].update(requested[section])
        if snippet and not {"title", "categoryId"} <= body["snippet"].keys():
            raise MetadataError("Current required snippet fields are unavailable.")
        # A privacy flip would silently discard an existing schedule. Require an
        # explicit separate policy decision rather than changing an unsupplied field.
        if body["status"].get("publishAt") and body["status"].get("privacyStatus") != "private":
            raise MetadataError("Privacy change would discard an unsupplied schedule.")
        try:
            client.update(body, part=",".join(key for key in body if key != "id"))
        except Exception:
            raise MetadataError("YouTube update failed; completion may be ambiguous.") from None
        result = {"video_id": video_id, "state": "verification_pending"}
        try:
            response = self.reads._list(
                client, "videos", part="snippet,status,contentDetails", id=video_id
            )
        except ReadError:
            return result
        if not response["items"]:
            return result
        items = response["items"]
        matches = len(items) == 1 and isinstance(items[0], dict)
        if matches:
            video = items[0]
            matches = video.get("id") == video_id and isinstance(video.get("snippet"), dict)
            matches = matches and video["snippet"].get("channelId") == profile.channel_id
            for section, fields in requested.items():
                actual = video.get(section, {})
                if not isinstance(actual, dict):
                    matches = False
                    continue
                for key, value in fields.items():
                    # Known API omission: Google Issue Tracker 581458347.
                    if key == "containsSyntheticMedia" and key not in actual:
                        continue
                    matches = matches and key in actual and actual[key] == value
        result["state"] = "succeeded" if matches else "mismatch"
        return result
