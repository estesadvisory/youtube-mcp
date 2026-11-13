"""Slice 3 acceptance with synthetic grants and a mutation-counting client."""

import pytest
from fastmcp import Client

from youtube_mcp.profiles import (
    YOUTUBE_FORCE_SSL,
    Credential,
    InMemoryCredentialProvider,
    Profile,
    ProfileError,
    ProfileStore,
)
from youtube_mcp.reads import ReadError, Reads
from youtube_mcp.server import create_server


class FakeClient:
    def __init__(self, channel):
        self.channel = channel
        self.channels = {"items": [{"id": channel}]}
        self.video = {"items": [{"id": "video", "snippet": {"channelId": channel}}]}
        self.calls = []
        self.mutations = []

    def list(self, resource, **parameters):
        self.calls.append((resource, parameters))
        if resource == "channels":
            return self.channels
        if resource == "videos":
            return self.video
        return {"items": [], "nextPageToken": "next"}

    def mutate(self, video):
        self.mutations.append(video)


@pytest.fixture
def setup():
    profiles = ProfileStore(
        Profile(name, channel, "Same label", frozenset({YOUTUBE_FORCE_SSL}), name + "-ref")
        for name, channel in [("first", "UC-first"), ("second", "UC-second")]
    )
    credentials = InMemoryCredentialProvider(
        {name + "-ref": Credential(name.encode()) for name in ("first", "second")}
    )
    clients = {name: FakeClient("UC-" + name) for name in ("first", "second")}
    selected = []

    def factory(credential):
        name = credential.value.decode()
        selected.append(name)
        return clients[name]

    return Reads(profiles, credentials, factory), clients, selected


def test_reads_select_each_profile_and_exact_api_parameters(setup):
    reads, clients, selected = setup
    for name in ("first", "second", "first"):
        assert reads.channel(name)["items"] == [{"id": "UC-" + name}]
        reads.require_owned_video(name, "video")
        reads.playlists(name, "cursor")
        reads.playlist_items(name, "playlist", "cursor")
        calls = clients[name].calls
        assert ("channels", {"part": "snippet,contentDetails", "mine": True}) in calls
        assert ("videos", {"part": "snippet,status,contentDetails", "id": "video"}) in calls
        assert (
            "playlists",
            dict(
                part="snippet,contentDetails",
                channelId="UC-" + name,
                maxResults=50,
                pageToken="cursor",
            ),
        ) in calls
        assert (
            "playlistItems",
            dict(
                part="snippet,contentDetails",
                playlistId="playlist",
                maxResults=50,
                pageToken="cursor",
            ),
        ) in calls
    assert selected == ["first"] * 4 + ["second"] * 4 + ["first"] * 4


@pytest.mark.parametrize("selection", [None, "", "unknown", "Same label"])
def test_invalid_profile_makes_no_client_calls(setup, selection):
    reads, clients, selected = setup
    with pytest.raises(ProfileError):
        reads.channel(selection)
    assert selected == []
    assert all(not client.calls for client in clients.values())


@pytest.mark.parametrize(
    "channels",
    [
        {"items": []},
        {"items": [{"id": "UC-other"}]},
        {"items": [{"id": "UC-first"}, {"id": "UC-first"}]},
        {"items": [{"id": "UC-first"}], "nextPageToken": "more"},
        {"items": [{"id": "UC-first"}], "pageInfo": {"totalResults": 2}},
        {"items": [None]},
        {},
    ],
)
def test_channel_failures_block_all_downstream_reads_and_mutations(setup, channels):
    reads, clients, _ = setup
    client = clients["first"]
    client.channels = channels
    for operation in [
        lambda: reads.channel("first"),
        lambda: reads.require_owned_video("first", "video"),
        lambda: reads.playlists("first"),
        lambda: reads.playlist_items("first", "playlist"),
    ]:
        with pytest.raises(ReadError):
            client.mutate(operation())
    assert client.mutations == []
    assert all(resource == "channels" for resource, _ in client.calls)
    assert clients["second"].calls == []


@pytest.mark.parametrize(
    "video",
    [
        {"items": []},
        {"items": [{"id": "video", "snippet": {"channelId": "UC-second"}}]},
        {"items": [{"id": "video"}]},
        {"items": [{"id": "video", "snippet": None}]},
        {"items": [{"id": "other", "snippet": {"channelId": "UC-first"}}]},
        {"items": [None]},
        {"items": [{}, {}]},
    ],
)
def test_video_mismatches_never_reach_mutation(setup, video):
    reads, clients, _ = setup
    clients["first"].video = video
    with pytest.raises(ReadError):
        clients["first"].mutate(reads.require_owned_video("first", "video"))
    assert all(client.mutations == [] for client in clients.values())


async def test_tools_dispatch_and_unconfigured_failure(setup):
    reads, _, _ = setup
    async with Client(create_server(reads)) as client:
        for tool, arguments in [
            ("get_channel", {}),
            ("get_video", {"video_id": "video"}),
            ("list_playlists", {}),
            ("list_playlist_items", {"playlist_id": "playlist"}),
        ]:
            result = await client.call_tool(tool, {"profile_id": "second", **arguments})
            assert not result.is_error
    async with Client(create_server()) as client:
        result = await client.call_tool(
            "get_channel", {"profile_id": "first"}, raise_on_error=False
        )
        assert result.is_error


def test_upstream_error_is_redacted_and_never_falls_back(setup):
    reads, clients, selected = setup

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-secret")

    clients["first"].list = fail
    with pytest.raises(ReadError) as failure:
        reads.channel("first")
    assert str(failure.value) == "YouTube read failed."
    assert failure.value.__suppress_context__
    assert selected == ["first"]
    assert clients["second"].calls == []
