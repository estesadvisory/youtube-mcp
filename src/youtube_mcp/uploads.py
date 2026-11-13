"""Durable offline-injectable upload jobs (Refs portfolio-ops#285).

The injected client implements begin_insert(body, size, job_id),
probe_insert(session), and upload_chunk(session, offset, data, total).
Probe/chunk return {offset: int, video_id: str | None}; probe is read-only.
Adapters must never log session URLs. No Google adapter ships in this core.
"""

import json
import os
import sqlite3
import stat
from pathlib import Path
from uuid import UUID, uuid4

from youtube_mcp.reads import Reads


class UploadError(ValueError):
    """Fixed-message, secret-safe upload failure."""


class JobStore:
    """Private SQLite records; transactions serialize each job advancement.

    Deployment supplies a private state directory, outside any public checkout.
    Session URLs exist only in this store and transient adapter arguments.
    """

    def __init__(self, directory: Path):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise UploadError("Job directory must be private.")
        path = directory / "jobs.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
                raise UploadError("Job database must be a private regular file.")
        finally:
            os.close(fd)
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT)")

    def save(self, job):
        self.connection.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job["job_id"], json.dumps(job))
        )

    def load(self, job_id):
        try:
            UUID(job_id)
        except (ValueError, TypeError, AttributeError):
            raise UploadError("Unknown upload job.") from None
        row = self.connection.execute("SELECT body FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise UploadError("Unknown upload job.")
        return json.loads(row[0])


class Uploads:
    def __init__(self, reads: Reads, store: JobStore, media_roots: dict[str, Path],
                 chunk_size: int = 256 * 1024):
        if chunk_size <= 0:
            raise UploadError("Chunk size must be positive.")
        self.reads, self.store = reads, store
        self.roots = {key: Path(value).resolve(strict=True) for key, value in media_roots.items()}
        self.chunk_size = chunk_size

    def _media(self, profile_id, media_path):
        if not isinstance(media_path, str) or ":" in media_path:
            raise UploadError("Media must be an allowed local file, never a URL.")
        root = self.roots.get(profile_id)
        if root is None:
            raise UploadError("Media directory is not configured for this profile.")
        try:
            path = Path(media_path)
            path = (root / path).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError
            # Resolve symlinks before opening and prohibit replacement of the final file.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            stream = os.fdopen(fd, "rb")
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                stream.close()
                raise ValueError
            return stream
        except (OSError, ValueError):
            raise UploadError("Media is outside the allowed directory or unavailable.") from None

    @staticmethod
    def _public(job):
        return {key: job[key] for key in ("job_id", "state", "video_id")}

    def start(self, profile_id: str, media_path: str, snippet: dict,
              privacy_status: str, self_declared_made_for_kids: bool,
              contains_synthetic_media: bool, publish_at: str | None = None):
        self.reads._profiles.resolve(profile_id)
        if privacy_status not in ("private", "unlisted", "public"):
            raise UploadError("Explicit valid privacyStatus is required.")
        if publish_at is not None and (privacy_status != "private" or not publish_at):
            raise UploadError("publishAt requires private privacyStatus.")
        if type(self_declared_made_for_kids) is not bool or type(contains_synthetic_media) is not bool:
            raise UploadError("Audience and synthetic media declarations must be booleans.")
        if (not isinstance(snippet, dict) or
                not {"title", "description", "categoryId"} <= snippet.keys() or
                not snippet.keys() <= {"title", "description", "categoryId", "tags", "defaultLanguage"}):
            raise UploadError("Explicit title, description and categoryId are required.")
        with self._media(profile_id, media_path) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size == 0:
                raise UploadError("Media must not be empty.")
        self.reads._bound(profile_id)
        status = {"privacyStatus": privacy_status,
                  "selfDeclaredMadeForKids": self_declared_made_for_kids,
                  "containsSyntheticMedia": contains_synthetic_media}
        if publish_at is not None:
            status["publishAt"] = publish_at
        job = dict(job_id=str(uuid4()), profile_id=profile_id, media_path=media_path,
                   size=size, body={"snippet": snippet, "status": status}, state="ready",
                   session=None, offset=0, video_id=None)
        self.store.save(job)
        return self._public(job)

    def advance(self, profile_id: str, job_id: str):
        db = self.store.connection
        db.execute("BEGIN IMMEDIATE")
        try:
            job = self.store.load(job_id)
            if job["profile_id"] != profile_id:
                raise UploadError("Job does not belong to this profile.")
            profile, client, _ = self.reads._bound(profile_id)
            if job["state"] in ("succeeded", "mismatch"):
                return self._public(job)
            if job["video_id"]:
                return self._verify(profile, client, job)
            with self._media(profile_id, job["media_path"]) as stream:
                if os.fstat(stream.fileno()).st_size != job["size"]:
                    raise UploadError("Media changed since job creation.")
                if not job["session"]:
                    if job["state"] == "ambiguous":
                        # Session creation itself may have landed: never insert twice.
                        return self._public(job)
                    job["state"] = "ambiguous"
                    self.store.save(job)
                    db.execute("COMMIT")
                    try:
                        session = client.begin_insert(job["body"], job["size"], job_id)
                        if not isinstance(session, str) or not session:
                            raise ValueError
                    except Exception:
                        return self._public(job)
                    db.execute("BEGIN IMMEDIATE")
                    job["session"] = session
                    self.store.save(job)
                try:
                    # Always discover server offset/completion before sending more bytes,
                    # including after restart or a lost final response. Never re-insert.
                    progress = client.probe_insert(job["session"])
                    self._progress(job, progress)
                    if not job["video_id"]:
                        stream.seek(job["offset"])
                        data = stream.read(self.chunk_size)
                        job["state"] = "ambiguous"
                        self.store.save(job)
                        db.execute("COMMIT")
                        progress = client.upload_chunk(job["session"], job["offset"], data, job["size"])
                        db.execute("BEGIN IMMEDIATE")
                        self._progress(job, progress)
                except Exception:
                    if not db.in_transaction:
                        db.execute("BEGIN IMMEDIATE")
                    job["state"] = "ambiguous"
                    self.store.save(job)
                    return self._public(job)
            if job["video_id"]:
                return self._verify(profile, client, job)
            job["state"] = "uploading"
            self.store.save(job)
            return self._public(job)
        finally:
            if db.in_transaction:
                db.execute("COMMIT")

    @staticmethod
    def _progress(job, progress):
        offset = progress["offset"]
        if type(offset) is not int or not 0 <= offset <= job["size"]:
            raise ValueError
        video_id = progress.get("video_id")
        if video_id is not None and (not isinstance(video_id, str) or not video_id.isalnum()):
            raise ValueError
        job["offset"], job["video_id"] = offset, video_id

    def _verify(self, profile, client, job):
        job["state"] = "verification_pending"
        self.store.save(job)
        try:
            response = self.reads._list(client, "videos", part="snippet,status,contentDetails",
                                        id=job["video_id"])
        except Exception:
            return self._public(job)
        items = response["items"]
        if not items:
            return self._public(job)
        expected = job["body"]
        matches = len(items) == 1 and isinstance(items[0], dict)
        if matches:
            video = items[0]
            matches = (video.get("id") == job["video_id"] and
                       isinstance(video.get("snippet"), dict) and
                       video["snippet"].get("channelId") == profile.channel_id)
            for section in ("snippet", "status"):
                actual = video.get(section, {})
                matches = matches and isinstance(actual, dict)
                for key, value in expected[section].items():
                    if key == "containsSyntheticMedia" and key not in actual:
                        continue
                    matches = matches and actual.get(key) == value
        job["state"] = "succeeded" if matches else "mismatch"
        self.store.save(job)
        return self._public(job)
