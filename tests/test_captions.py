"""Slice 7 fake-only acceptance; no Google calls."""

import pytest

from youtube_mcp.captions import CaptionError, CaptionExists, Captions
from youtube_mcp.profiles import (
    YOUTUBE_FORCE_SSL,
    Credential,
    InMemoryCredentialProvider,
    Profile,
    ProfileError,
    ProfileStore,
)
from youtube_mcp.reads import ReadError, Reads
from youtube_mcp.uploads import UploadError


class Fake:
    def __init__(self, channel):
        self.channel = self.owner = channel
        self.calls = []
        self.tracks = None
        self.failure = None

    def list(self, resource, **kwargs):
        self.calls.append((resource, kwargs))
        if resource == "channels":
            return {"items": [{"id": self.channel}]}
        if resource == "videos":
            return {"items": [{"id": kwargs["id"], "snippet": {"channelId": self.owner}}]}
        assert resource == "captions"
        if isinstance(self.tracks, Exception):
            raise self.tracks
        return {"items": self.tracks}

    def insert_caption(self, body, media, *, part):
        self.calls.append(("insert", body, media, part))
        if self.failure:
            raise self.failure
        if self.tracks is None:
            self.tracks = [{"id": "track", **body}]
        return {"id": "track"}


@pytest.fixture
def rig(tmp_path):
    profiles = ProfileStore(
        Profile(n, n, n, frozenset({YOUTUBE_FORCE_SSL}), n) for n in ("one", "two")
    )
    creds = InMemoryCredentialProvider({n: Credential(n.encode()) for n in ("one", "two")})
    clients = {n: Fake(n) for n in ("one", "two")}
    roots = {}
    for n in clients:
        roots[n] = tmp_path / n
        roots[n].mkdir()
        (roots[n] / "track.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    return Captions(Reads(profiles, creds, lambda c: clients[c.value.decode()]), roots), clients


def insert(tool, profile="one"):
    return tool.insert(profile, "video", "en", "English", False, "track.srt")


def test_insert_and_verify(rig):
    tool, clients = rig
    assert insert(tool)["state"] == "succeeded"
    calls = clients["one"].calls
    assert [c[0] for c in calls] == ["channels", "videos", "insert", "captions"]
    assert calls[2][1] == {
        "snippet": {
            "videoId": "video",
            "language": "en",
            "name": "English",
            "isDraft": False,
        }
    }
    assert calls[2][2].startswith(b"1\n")
    assert calls[2][3] == "snippet"
    assert calls[3][1] == {"part": "snippet", "videoId": "video"}


@pytest.mark.parametrize("field", ["channel", "owner"])
def test_guards(rig, field):
    tool, clients = rig
    setattr(clients["one"], field, "foreign")
    with pytest.raises(ReadError):
        insert(tool)
    assert not any(c[0] == "insert" for c in clients["one"].calls)


def test_conflict_no_repair(rig):
    tool, clients = rig
    clients["one"].failure = CaptionExists("untrusted secret")
    assert insert(tool) == {"video_id": "video", "state": "captionExists", "status": 409}
    assert [c[0] for c in clients["one"].calls] == ["channels", "videos", "insert"]


@pytest.mark.parametrize("path", ["../two/track.srt", "https://example.invalid/a", "file:///a"])
def test_confinement(rig, path):
    tool, clients = rig
    with pytest.raises(UploadError):
        tool.insert("one", "video", "en", "English", False, path)
    assert clients["one"].calls == []


def test_symlink_escape(rig):
    tool, clients = rig
    (tool.roots["one"] / "escape").symlink_to(tool.roots["two"] / "track.srt")
    with pytest.raises(UploadError):
        tool.insert("one", "video", "en", "English", False, "escape")
    assert clients["one"].calls == []


@pytest.mark.parametrize("tracks", [[], RuntimeError("secret"), [{"id": "other"}]])
def test_pending(rig, tracks):
    tool, clients = rig
    clients["one"].tracks = tracks
    assert insert(tool)["state"] == "verification_pending"


@pytest.mark.parametrize("field", ["videoId", "language", "name", "isDraft"])
def test_observed_mismatch(rig, field):
    tool, clients = rig
    snippet = dict(videoId="video", language="en", name="English", isDraft=False)
    snippet[field] = True if field == "isDraft" else "wrong"
    clients["one"].tracks = [{"id": "track", "snippet": snippet}]
    assert insert(tool)["state"] == "mismatch"


def test_profiles(rig):
    tool, clients = rig
    for profile in (None, "", "unknown"):
        with pytest.raises(ProfileError):
            insert(tool, profile)
    for profile in clients:
        assert insert(tool, profile)["state"] == "succeeded"
        assert len(clients[profile].calls) == 4


def test_missing_scope(rig):
    tool, clients = rig
    tool.reads._profiles = ProfileStore([Profile("one", "one", "one", frozenset(), "one")])
    with pytest.raises(ProfileError):
        insert(tool)
    assert clients["one"].calls == []


@pytest.mark.parametrize(
    "args",
    [
        ("", "en", "English", False),
        ("video", "", "English", False),
        ("video", "en", "", False),
        ("video", "en", "English", 0),
    ],
)
def test_explicit_payload(rig, args):
    tool, clients = rig
    with pytest.raises(CaptionError):
        tool.insert("one", *args, "track.srt")
    assert clients["one"].calls == []


def test_ambiguous_no_retry(rig):
    tool, clients = rig
    clients["one"].failure = RuntimeError("secret")
    assert insert(tool)["state"] == "ambiguous"
    assert sum(c[0] == "insert" for c in clients["one"].calls) == 1


async def test_registration(rig):
    from fastmcp import Client

    from youtube_mcp.server import create_server

    tool, _ = rig
    args = dict(
        profile_id="one",
        video_id="video",
        language="en",
        name="English",
        is_draft=False,
        media_path="track.srt",
    )
    async with Client(create_server(captions=tool)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools["insert_caption"].inputSchema["required"]) == set(args)
        assert (await client.call_tool("insert_caption", args)).data["state"] == "succeeded"
    async with Client(create_server()) as client:
        assert (await client.call_tool("insert_caption", args, raise_on_error=False)).is_error
