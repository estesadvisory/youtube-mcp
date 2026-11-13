"""Slice 4 fake-only acceptance; fault streams contain just six bytes."""

import copy
import os
from pathlib import Path

import pytest

from youtube_mcp.profiles import (
    YOUTUBE_FORCE_SSL,
    Credential,
    InMemoryCredentialProvider,
    Profile,
    ProfileStore,
)
from youtube_mcp.reads import ReadError, Reads
from youtube_mcp.uploads import JobStore, UploadError, Uploads


class FakeUploadClient:
    def __init__(self, channel):
        self.channel = channel
        self.calls = []
        self.offset = 0
        self.video_id = None
        self.body = None
        self.visible = True
        self.lost_reply = False
        self.mismatch = False

    def list(self, resource, **parameters):
        self.calls.append(resource)
        if resource == "channels":
            return {"items": [{"id": self.channel}]}
        if not self.visible:
            return {"items": []}
        video = copy.deepcopy(self.body)
        video["id"] = self.video_id
        video["snippet"]["channelId"] = self.channel
        video["status"].pop("containsSyntheticMedia")
        if self.mismatch:
            video["snippet"]["title"] = "wrong"
        return {"items": [video]}

    def begin_insert(self, body, size, job_id):
        self.calls.append("insert")
        self.body = copy.deepcopy(body)
        return "https://secret.invalid/resumable?token=synthetic-secret"

    def probe_insert(self, session):
        self.calls.append("lookup")
        return {"offset": self.offset, "video_id": self.video_id}

    def upload_chunk(self, session, offset, data, total):
        self.calls.append("chunk")
        assert offset == self.offset
        self.offset += len(data)
        if self.offset == total:
            self.video_id = "video1"
        if self.lost_reply:
            raise OSError("synthetic-secret")
        return {"offset": self.offset, "video_id": self.video_id}


@pytest.fixture
def rig(tmp_path):
    profiles = ProfileStore(
        Profile(name, "UC-" + name, name, frozenset({YOUTUBE_FORCE_SSL}), name)
        for name in ("one", "two")
    )
    credentials = InMemoryCredentialProvider(
        {name: Credential(name.encode()) for name in ("one", "two")}
    )
    clients = {name: FakeUploadClient("UC-" + name) for name in ("one", "two")}
    reads = Reads(profiles, credentials, lambda credential: clients[credential.value.decode()])
    media = tmp_path / "media"
    media.mkdir()
    (media / "tiny.mp4").write_bytes(b"123456")
    state = tmp_path / "state"
    roots = {name: media for name in clients}
    uploads = Uploads(reads, JobStore(state), roots, chunk_size=3)
    return uploads, clients, state, roots


def start(uploads, profile="one", **overrides):
    arguments = dict(
        profile_id=profile,
        media_path="tiny.mp4",
        snippet={"title": "title", "description": "description", "categoryId": "22"},
        privacy_status="private",
        self_declared_made_for_kids=False,
        contains_synthetic_media=True,
        publish_at="2027-01-01T00:00:00Z",
    )
    arguments.update(overrides)
    return uploads.start(**arguments)


def test_resumable_flow_and_explicit_write_fields(rig):
    uploads, clients, _, _ = rig
    job = start(uploads)
    assert job["state"] == "ready"
    assert uploads.advance("one", job["job_id"])["state"] == "uploading"
    result = uploads.advance("one", job["job_id"])
    assert result["state"] == "succeeded"
    assert set(result) == {"job_id", "state", "video_id"}
    assert clients["one"].body["status"] == {
        "privacyStatus": "private",
        "publishAt": "2027-01-01T00:00:00Z",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": True,
    }
    assert clients["one"].calls.count("insert") == 1
    uploads.advance("one", job["job_id"])
    assert clients["one"].calls.count("chunk") == 2


def test_restart_resumes_secret_session(rig, caplog, capsys):
    uploads, clients, state, roots = rig
    job = start(uploads)
    uploads.advance("one", job["job_id"])
    uploads.store.connection.close()
    restarted = Uploads(uploads.reads, JobStore(state), roots, chunk_size=3)
    result = restarted.advance("one", job["job_id"])
    assert result["state"] == "succeeded"
    assert clients["one"].calls.count("insert") == 1
    assert "synthetic-secret" not in repr(result) + caplog.text + str(capsys.readouterr())
    assert os.stat(state).st_mode & 0o077 == 0
    assert all(os.stat(path).st_mode & 0o077 == 0 for path in state.iterdir())


def test_ambiguous_completion_looks_up_before_sending_again(rig):
    uploads, clients, state, roots = rig
    client = clients["one"]
    job = start(uploads)
    uploads.advance("one", job["job_id"])
    client.lost_reply = True
    assert uploads.advance("one", job["job_id"])["state"] == "ambiguous"
    client.calls.clear()
    restarted = Uploads(uploads.reads, JobStore(state), roots, chunk_size=3)
    assert restarted.advance("one", job["job_id"])["state"] == "succeeded"
    assert client.calls == ["channels", "lookup", "videos"]


@pytest.mark.parametrize(
    "visible,mismatch,expected",
    [
        (False, False, "verification_pending"),
        (True, True, "mismatch"),
        (True, False, "succeeded"),
    ],
)
def test_pending_is_distinct_from_mismatch_and_synthetic_omission(rig, visible, mismatch, expected):
    uploads, clients, _, _ = rig
    job = start(uploads)
    uploads.advance("one", job["job_id"])
    clients["one"].visible = visible
    clients["one"].mismatch = mismatch
    assert uploads.advance("one", job["job_id"])["state"] == expected
    if not visible:
        clients["one"].visible = True
        assert uploads.advance("one", job["job_id"])["state"] == "succeeded"


def test_bound_guard_zero_mutations(rig):
    uploads, clients, _, _ = rig
    job = start(uploads)
    clients["one"].channel = "wrong"
    with pytest.raises(ReadError):
        uploads.advance("one", job["job_id"])
    with pytest.raises(ReadError):
        start(uploads)
    assert not {"insert", "chunk"} & set(clients["one"].calls)


@pytest.mark.parametrize(
    "arguments",
    [
        {"privacy_status": "public"},
        {"privacy_status": "unlisted"},
        {"privacy_status": ""},
        {"self_declared_made_for_kids": None},
        {"contains_synthetic_media": 1},
    ],
)
def test_invalid_status_refused_without_calls(rig, arguments):
    uploads, clients, _, _ = rig
    with pytest.raises(UploadError):
        start(uploads, **arguments)
    assert clients["one"].calls == []


@pytest.mark.parametrize(
    "media", ["../outside.mp4", "https://example.com/file", "file:///etc/passwd"]
)
def test_media_escape_and_url_refused(rig, media):
    uploads, clients, _, roots = rig
    (roots["one"].parent / "outside.mp4").write_bytes(b"x")
    with pytest.raises(UploadError):
        start(uploads, media_path=media)
    assert clients["one"].calls == []


def test_symlink_escape_refused(rig):
    uploads, _, _, roots = rig
    (roots["one"] / "escape").symlink_to(Path("/etc/passwd"))
    with pytest.raises(UploadError):
        start(uploads, media_path="escape")


def test_two_profile_job_isolation(rig):
    uploads, clients, _, _ = rig
    first = start(uploads)
    with pytest.raises(UploadError):
        uploads.advance("two", first["job_id"])
    assert clients["two"].calls == []
    second = start(uploads, "two")
    for name, job in [("one", first), ("two", second)]:
        uploads.advance(name, job["job_id"])
        assert uploads.advance(name, job["job_id"])["state"] == "succeeded"
        assert clients[name].calls.count("insert") == 1


async def test_mcp_upload_tools_required_fields_and_safe_output(rig):
    from fastmcp import Client

    from youtube_mcp.server import create_server

    uploads, _, _, _ = rig
    async with Client(create_server(uploads.reads, uploads)) as client:
        arguments = dict(
            profile_id="one",
            media_path="tiny.mp4",
            snippet={"title": "t", "description": "d", "categoryId": "22"},
            privacy_status="private",
            self_declared_made_for_kids=False,
            contains_synthetic_media=True,
        )
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert {"privacy_status", "self_declared_made_for_kids", "contains_synthetic_media"} <= set(
            tools["upload_video"].inputSchema["required"]
        )
        job = (await client.call_tool("upload_video", arguments)).data
        for _ in range(2):
            result = await client.call_tool(
                "resume_upload", {"profile_id": "one", "job_id": job["job_id"]}
            )
        assert result.data["state"] == "succeeded"
        assert "synthetic-secret" not in repr(result)
    async with Client(create_server()) as client:
        result = await client.call_tool("upload_video", arguments, raise_on_error=False)
        assert result.is_error


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("snippet", "title", "other"),
        ("snippet", "description", "other"),
        ("snippet", "categoryId", "99"),
        ("status", "privacyStatus", "public"),
        ("status", "publishAt", "2028-01-01T00:00:00Z"),
        ("status", "selfDeclaredMadeForKids", True),
        ("status", "containsSyntheticMedia", False),
    ],
)
def test_each_observed_field_mismatch_fails(rig, section, field, value):
    uploads, clients, _, _ = rig
    job = start(uploads)
    uploads.advance("one", job["job_id"])
    client = clients["one"]
    original = client.list

    def changed(resource, **parameters):
        response = original(resource, **parameters)
        if resource == "videos":
            response["items"][0][section][field] = value
        return response

    client.list = changed
    assert uploads.advance("one", job["job_id"])["state"] == "mismatch"


def test_lost_initial_session_response_never_reinserts(rig):
    uploads, clients, state, roots = rig
    job = start(uploads)
    client = clients["one"]

    def fail(*args):
        client.calls.append("insert")
        raise OSError("https://secret.invalid/token")

    client.begin_insert = fail
    assert uploads.advance("one", job["job_id"])["state"] == "ambiguous"
    restarted = Uploads(uploads.reads, JobStore(state), roots)
    assert restarted.advance("one", job["job_id"])["state"] == "ambiguous"
    assert client.calls.count("insert") == 1


def test_probe_failure_exposes_no_secret_and_sends_no_chunk(rig):
    uploads, clients, _, _ = rig
    job = start(uploads)
    client = clients["one"]

    def fail(*args):
        raise RuntimeError("synthetic-secret")

    client.probe_insert = fail
    result = uploads.advance("one", job["job_id"])
    assert result["state"] == "ambiguous"
    assert "synthetic-secret" not in repr(result)
    assert "chunk" not in client.calls


def test_missing_profile_and_media_root_never_fall_back(rig):
    from youtube_mcp.profiles import ProfileError

    uploads, clients, _, _ = rig
    for profile in (None, "unknown", ""):
        with pytest.raises(ProfileError):
            start(uploads, profile)
    uploads.roots.pop("one")
    with pytest.raises(UploadError):
        start(uploads)
    assert all(not client.calls for client in clients.values())


def test_changed_media_is_refused_before_insert(rig):
    uploads, clients, _, roots = rig
    job = start(uploads)
    (roots["one"] / "tiny.mp4").write_bytes(b"654321")
    with pytest.raises(UploadError):
        uploads.advance("one", job["job_id"])
    assert "insert" not in clients["one"].calls


def test_insecure_or_symlinked_job_storage_refused(tmp_path):
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(UploadError):
        JobStore(insecure)
    linked = tmp_path / "linked"
    linked.symlink_to(insecure, target_is_directory=True)
    with pytest.raises(UploadError):
        JobStore(linked)


def test_profile_rebinding_cannot_resume_old_channel_job(rig):
    uploads, clients, _, _ = rig
    job = start(uploads)
    profiles = ProfileStore(
        [Profile("one", "new-channel", "one", frozenset({YOUTUBE_FORCE_SSL}), "one")]
    )
    uploads.reads._profiles = profiles
    clients["one"].channel = "new-channel"
    with pytest.raises(UploadError, match="Job channel"):
        uploads.advance("one", job["job_id"])
    assert "insert" not in clients["one"].calls


def test_partial_lost_reply_resumes_at_server_offset(rig):
    uploads, clients, state, roots = rig
    job = start(uploads)
    client = clients["one"]
    client.lost_reply = True
    assert uploads.advance("one", job["job_id"])["state"] == "ambiguous"
    assert client.offset == 3
    client.lost_reply = False
    restarted = Uploads(uploads.reads, JobStore(state), roots, chunk_size=3)
    assert restarted.advance("one", job["job_id"])["state"] == "succeeded"
    assert client.calls.count("insert") == 1


def test_concurrent_store_instances_send_each_chunk_once(rig):
    from concurrent.futures import ThreadPoolExecutor

    uploads, clients, state, roots = rig
    job = start(uploads)

    def advance():
        store = JobStore(state)
        try:
            return Uploads(uploads.reads, store, roots, chunk_size=3).advance("one", job["job_id"])
        finally:
            store.connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: advance(), range(2)))
    assert {result["state"] for result in results} == {"uploading", "succeeded"}
    assert clients["one"].calls.count("insert") == 1
    assert clients["one"].calls.count("chunk") == 2
