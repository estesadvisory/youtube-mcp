"""Slice 5 acceptance: fake clients only (Refs portfolio-ops#285)."""

from copy import deepcopy

import pytest
from fastmcp import Client
from test_reads import setup  # noqa: F401, F811

from youtube_mcp.metadata import Metadata, MetadataError
from youtube_mcp.reads import ReadError
from youtube_mcp.server import create_server


@pytest.fixture
def context(setup):  # noqa: F811
    reads, clients, selected = setup
    for client in clients.values():
        client.video["items"][0].update(
            snippet={
                "channelId": client.channel,
                "title": "old",
                "categoryId": "22",
                "description": "keep",
                "tags": ["keep"],
                "defaultLanguage": "en",
            },
            status={
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
                "embeddable": True,
                "license": "youtube",
                "publicStatsViewable": False,
            },
        )

        def update(body, *, part, client=client):
            client.mutations.append((deepcopy(body), part))
            # Emulate part replacement, not a misleading patch fake.
            for section in part.split(","):
                channel = client.video["items"][0]["snippet"]["channelId"]
                client.video["items"][0][section] = deepcopy(body[section])
                client.video["items"][0]["snippet"]["channelId"] = channel
            client.video["items"][0]["status"].pop("containsSyntheticMedia", None)
            if getattr(client, "mismatch", False):
                client.video["items"][0]["snippet"]["title"] = "wrong"
            if getattr(client, "pending", False):
                client.video = {"items": []}
            if getattr(client, "read_error", False):
                client.list = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret"))
            return {}

        client.update = update
    return Metadata(reads), clients, selected


def test_partial_update_preserves_unsupplied_values(context):
    metadata, clients, _ = context
    before = deepcopy(clients["first"].video["items"][0])
    result = metadata.update("first", "video", {"title": "new"}, {"selfDeclaredMadeForKids": False})
    assert result["state"] == "succeeded"
    after = clients["first"].video["items"][0]
    before["snippet"]["title"] = "new"
    assert after == before
    body, part = clients["first"].mutations[0]
    assert part == "snippet,status"
    assert "channelId" not in body["snippet"]


@pytest.mark.parametrize("privacy", ["private", "unlisted"])
def test_privacy_flip_and_profile_isolation(context, privacy):
    metadata, clients, selected = context
    for name in ("first", "second", "first"):
        clients[name].video["items"][0]["status"]["privacyStatus"] = privacy
        assert (
            metadata.update(
                name,
                "video",
                {},
                {
                    "selfDeclaredMadeForKids": True,
                    "privacyStatus": "public",
                    "containsSyntheticMedia": True,
                },
            )["state"]
            == "succeeded"
        )
    assert selected == ["first", "second", "first"]
    assert len(clients["first"].mutations) == 2
    assert len(clients["second"].mutations) == 1
    assert all(part == "status" for _, part in clients["first"].mutations)


def test_schedule(context):
    metadata, clients, _ = context
    assert (
        metadata.update(
            "first",
            "video",
            {},
            {
                "selfDeclaredMadeForKids": False,
                "privacyStatus": "private",
                "publishAt": "2027-01-01T00:00:00Z",
            },
        )["state"]
        == "succeeded"
    )
    assert clients["first"].mutations[0][0]["status"]["privacyStatus"] == "private"


@pytest.mark.parametrize(
    "status",
    [
        {},
        {"selfDeclaredMadeForKids": None},
        {"selfDeclaredMadeForKids": False, "publishAt": "2027-01-01T00:00:00Z"},
        {"selfDeclaredMadeForKids": False, "publishAt": "later", "privacyStatus": "public"},
        {"selfDeclaredMadeForKids": False, "containsSyntheticMedia": "false"},
        {"selfDeclaredMadeForKids": False, "privacyStatus": "other"},
    ],
)
def test_invalid_request_zero_mutations(context, status):
    metadata, clients, selected = context
    with pytest.raises(MetadataError):
        metadata.update("first", "video", {}, status)
    assert selected == []
    assert all(not client.mutations for client in clients.values())


@pytest.mark.parametrize("guard", ["channel", "owner"])
def test_guard_zero_mutations(context, guard):
    metadata, clients, _ = context
    if guard == "channel":
        clients["first"].channels = {"items": [{"id": "wrong"}]}
    else:
        clients["first"].video["items"][0]["snippet"]["channelId"] = "wrong"
    with pytest.raises(ReadError):
        metadata.update("first", "video", {}, {"selfDeclaredMadeForKids": False})
    assert all(not client.mutations for client in clients.values())


@pytest.mark.parametrize(
    "fault,state",
    [
        ("mismatch", "mismatch"),
        ("pending", "verification_pending"),
        ("read_error", "verification_pending"),
    ],
)
def test_readback_states(context, fault, state):
    metadata, clients, _ = context
    setattr(clients["first"], fault, True)
    assert (
        metadata.update(
            "first",
            "video",
            {"title": "new"},
            {
                "selfDeclaredMadeForKids": False,
            },
        )["state"]
        == state
    )
    assert len(clients["first"].mutations) == 1


async def test_tool(context):
    metadata, _, _ = context
    arguments = dict(
        profile_id="first", video_id="video", snippet={}, status={"selfDeclaredMadeForKids": False}
    )
    async with Client(create_server(metadata=metadata)) as client:
        result = await client.call_tool("update_video", arguments)
        assert result.data["state"] == "succeeded"
    async with Client(create_server()) as client:
        result = await client.call_tool("update_video", arguments, raise_on_error=False)
        assert result.is_error


def test_all_requested_fields(context):
    metadata, _, _ = context
    assert (
        metadata.update(
            "first",
            "video",
            {
                "title": "new",
                "description": "",
                "tags": [],
                "categoryId": "10",
                "defaultLanguage": "fr",
            },
            {"selfDeclaredMadeForKids": True, "containsSyntheticMedia": False},
        )["state"]
        == "succeeded"
    )


@pytest.mark.parametrize("selection", [None, "", "missing", "Same label"])
def test_explicit_profile_only(context, selection):
    from youtube_mcp.profiles import ProfileError

    metadata, clients, selected = context
    with pytest.raises(ProfileError):
        metadata.update(selection, "video", {}, {"selfDeclaredMadeForKids": False})
    assert selected == []
    assert all(not client.mutations for client in clients.values())


def test_update_error_is_safe(context):
    metadata, clients, _ = context

    def fail(*args, **kwargs):
        raise RuntimeError("secret")

    clients["first"].update = fail
    with pytest.raises(MetadataError, match="completion may be ambiguous") as failure:
        metadata.update("first", "video", {}, {"selfDeclaredMadeForKids": False})
    assert "secret" not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("privacy", ["public", "unlisted"])
def test_cannot_schedule_nonprivate_video(context, privacy):
    metadata, clients, _ = context
    clients["first"].video["items"][0]["status"]["privacyStatus"] = privacy
    with pytest.raises(MetadataError):
        metadata.update(
            "first",
            "video",
            {},
            {
                "selfDeclaredMadeForKids": False,
                "privacyStatus": "private",
                "publishAt": "later",
            },
        )
    assert not clients["first"].mutations
