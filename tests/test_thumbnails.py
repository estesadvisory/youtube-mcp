"""Accepted plan Slice 6; no Google adapter or network calls."""

import pytest

from youtube_mcp.profiles import (
    YOUTUBE_FORCE_SSL,
    Credential,
    InMemoryCredentialProvider,
    Profile,
    ProfileError,
    ProfileStore,
)
from youtube_mcp.reads import ReadError, Reads
from youtube_mcp.thumbnails import ThumbnailError, Thumbnails
from youtube_mcp.uploads import UploadError


class Fake:
    def __init__(self, channel):
        self.channel = self.owner = channel
        self.calls = []
        self.response = {"items": [{"default": {"url": "https://example.invalid/thumb"}}]}
        self.thumbnails = {}
        self.fail = False

    def list(self, resource, **kwargs):
        self.calls.append(resource)
        if resource == "channels":
            return {"items": [{"id": self.channel}]}
        return {
            "items": [
                {
                    "id": kwargs["id"],
                    "snippet": {
                        "channelId": self.owner,
                        "thumbnails": self.thumbnails,
                    },
                }
            ]
        }

    def set_thumbnail(self, video_id, media, mime):
        self.calls.append(("set", video_id, media, mime))
        if self.fail:
            raise RuntimeError("secret")
        return self.response


@pytest.fixture
def rig(tmp_path):
    profiles = ProfileStore(
        Profile(n, n, n, frozenset({YOUTUBE_FORCE_SSL}), n) for n in ("one", "two")
    )
    credentials = InMemoryCredentialProvider({n: Credential(n.encode()) for n in ("one", "two")})
    clients = {n: Fake(n) for n in ("one", "two")}
    reads = Reads(profiles, credentials, lambda c: clients[c.value.decode()])
    roots = {}
    for n in clients:
        roots[n] = tmp_path / n
        roots[n].mkdir()
        (roots[n] / "thumb").write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    return Thumbnails(reads, roots), clients


@pytest.mark.parametrize(
    "media,mime", [(b"\x89PNG\r\n\x1a\nsmall", "image/png"), (b"\xff\xd8\xffsmall", "image/jpeg")]
)
def test_insert_and_set_response_verification(rig, media, mime):
    tool, clients = rig
    (tool.roots["one"] / "thumb").write_bytes(media)
    assert tool.set("one", "video", "thumb")["state"] == "succeeded"
    assert clients["one"].calls == ["channels", "videos", ("set", "video", media, mime)]


@pytest.mark.parametrize("field", ["channel", "owner"])
def test_guard_zero_mutations(rig, field):
    tool, clients = rig
    setattr(clients["one"], field, "wrong")
    with pytest.raises(ReadError):
        tool.set("one", "video", "thumb")
    assert all(isinstance(c, str) for c in clients["one"].calls)


@pytest.mark.parametrize("path", ["../two/thumb", "https://example.invalid/a", "file:///a"])
def test_media_confinement(rig, path):
    tool, clients = rig
    with pytest.raises(UploadError):
        tool.set("one", "video", path)
    assert clients["one"].calls == []


def test_symlink_escape(rig):
    tool, clients = rig
    (tool.roots["one"] / "escape").symlink_to(tool.roots["two"] / "thumb")
    with pytest.raises(UploadError):
        tool.set("one", "video", "escape")
    assert clients["one"].calls == []


@pytest.mark.parametrize("media", [b"GIF89a", b"", b"x" * (2 * 1024 * 1024 + 1)])
def test_invalid_media_zero_calls(rig, media):
    tool, clients = rig
    (tool.roots["one"] / "thumb").write_bytes(media)
    with pytest.raises(ThumbnailError):
        tool.set("one", "video", "thumb")
    assert clients["one"].calls == []


@pytest.mark.parametrize("visible", [False, True])
def test_readback_fallback(rig, visible):
    tool, clients = rig
    clients["one"].response = {}
    if visible:
        clients["one"].thumbnails = {"default": {"url": "https://example.invalid/read"}}
    result = tool.set("one", "video", "thumb")
    assert result["state"] == ("succeeded" if visible else "verification_pending")
    assert clients["one"].calls[-1] == "videos"


def test_ambiguous_never_retries(rig):
    tool, clients = rig
    clients["one"].fail = True
    assert tool.set("one", "video", "thumb") == {"state": "ambiguous", "video_id": "video"}
    assert len([c for c in clients["one"].calls if isinstance(c, tuple)]) == 1


def test_profiles_isolated_and_explicit(rig):
    tool, clients = rig
    for profile in (None, "", "unknown"):
        with pytest.raises(ProfileError):
            tool.set(profile, "video", "thumb")
    assert all(not c.calls for c in clients.values())
    for profile in clients:
        assert tool.set(profile, "video", "thumb")["state"] == "succeeded"
        assert len(clients[profile].calls) == 3


async def test_registration(rig):
    from fastmcp import Client

    from youtube_mcp.server import create_server

    tool, _ = rig
    args = {"profile_id": "one", "video_id": "video", "media_path": "thumb"}
    async with Client(create_server(thumbnails=tool)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools["set_thumbnail"].inputSchema["required"]) == set(args)
        assert (await client.call_tool("set_thumbnail", args)).data["state"] == "succeeded"
    async with Client(create_server()) as client:
        assert (await client.call_tool("set_thumbnail", args, raise_on_error=False)).is_error
