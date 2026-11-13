"""Slice 8 acceptance with synthetic profiles only (Refs portfolio-ops#285)."""

from copy import deepcopy

import pytest
from fastmcp import Client
from test_reads import setup  # noqa: F401

from youtube_mcp.playlists import PlaylistError, Playlists
from youtube_mcp.profiles import ProfileError
from youtube_mcp.reads import ReadError
from youtube_mcp.server import create_server


@pytest.fixture
def context(setup):  # noqa: F811
    reads, clients, selected = setup
    for client in clients.values():
        client.playlist = {"items": [{"id": "playlist", "snippet": {"channelId": client.channel}}]}
        client.members = []
        client.fault = None
        original = client.list

        def listing(resource, client=client, original=original, **parameters):
            if resource == "playlists":
                return client.playlist
            if resource == "playlistItems":
                if client.mutations and client.fault == "pending":
                    raise RuntimeError("synthetic-secret")
                if client.mutations and client.fault == "malformed":
                    return {}
                return {"items": deepcopy(client.members)}
            return original(resource, **parameters)

        def insert(body, *, part, client=client):
            client.mutations.append((deepcopy(body), part))
            if client.fault != "mismatch":
                client.members.append(deepcopy(body))
            if client.fault == "ambiguous":
                raise RuntimeError("synthetic-secret")
            return {"items": [body]}

        client.list = listing
        client.insert_playlist_item = insert
    return Playlists(reads), clients, selected


def test_write_readback_and_repeat(context):
    playlists, clients, _ = context
    assert playlists.assign("first", "video", "playlist")["state"] == "succeeded"
    assert playlists.assign("first", "video", "playlist")["state"] == "already_present"
    assert clients["first"].mutations == [
        (
            {
                "snippet": {
                    "playlistId": "playlist",
                    "resourceId": {"kind": "youtube#video", "videoId": "video"},
                }
            },
            "snippet",
        )
    ]
    assert not clients["second"].mutations


@pytest.mark.parametrize("guard", ["channel", "video", "playlist", "missing"])
def test_guards_zero_writes(context, guard):
    playlists, clients, _ = context
    client = clients["first"]
    if guard == "channel":
        client.channels = {"items": [{"id": "wrong"}]}
    elif guard == "video":
        client.video["items"][0]["snippet"]["channelId"] = "wrong"
    elif guard == "playlist":
        client.playlist["items"][0]["snippet"]["channelId"] = "wrong"
    else:
        client.playlist = {"items": []}
    with pytest.raises((ReadError, PlaylistError)):
        playlists.assign("first", "video", "playlist")
    assert all(not client.mutations for client in clients.values())


@pytest.mark.parametrize(
    "fault,state",
    [
        ("mismatch", "mismatch"),
        ("pending", "verification_pending"),
        ("malformed", "verification_pending"),
        ("ambiguous", "ambiguous"),
    ],
)
def test_fault_states_no_retry(context, fault, state):
    playlists, clients, _ = context
    clients["first"].fault = fault
    assert playlists.assign("first", "video", "playlist")["state"] == state
    assert len(clients["first"].mutations) == 1
    if fault == "ambiguous":
        assert playlists.assign("first", "video", "playlist")["state"] == "already_present"
        assert len(clients["first"].mutations) == 1


@pytest.mark.parametrize("profile", [None, "", "missing", "Same label"])
def test_no_default(context, profile):
    playlists, clients, selected = context
    with pytest.raises(ProfileError):
        playlists.assign(profile, "video", "playlist")
    assert not selected
    assert all(not client.mutations for client in clients.values())


def test_profile_isolation(context):
    playlists, clients, selected = context
    for name in ("first", "second"):
        assert playlists.assign(name, "video", "playlist")["state"] == "succeeded"
    assert playlists.assign("first", "video", "playlist")["state"] == "already_present"
    assert selected == ["first", "second", "first"]
    assert all(len(client.mutations) == 1 for client in clients.values())


def test_failed_preflight_does_not_write(context):
    playlists, clients, _ = context
    original = clients["first"].list

    def fail(resource, **parameters):
        if resource == "playlistItems":
            raise RuntimeError("synthetic-secret")
        return original(resource, **parameters)

    clients["first"].list = fail
    with pytest.raises(ReadError, match="YouTube read failed"):
        playlists.assign("first", "video", "playlist")
    assert not clients["first"].mutations


async def test_tool(context):
    playlists, _, _ = context
    arguments = dict(profile_id="first", video_id="video", playlist_id="playlist")
    async with Client(create_server(playlists=playlists)) as client:
        assert (await client.call_tool("assign_playlist", arguments)).data["state"] == "succeeded"
    async with Client(create_server()) as client:
        assert (await client.call_tool("assign_playlist", arguments, raise_on_error=False)).is_error


def test_paginated_existing_membership(context):
    playlists, clients, _ = context
    original = clients["first"].list

    def pages(resource, **parameters):
        if resource != "playlistItems":
            return original(resource, **parameters)
        assert parameters["playlistId"] == "playlist"
        assert parameters["videoId"] == "video"
        if "pageToken" not in parameters:
            return {"items": [], "nextPageToken": "next"}
        assert parameters["pageToken"] == "next"
        return {
            "items": [
                {
                    "snippet": {
                        "playlistId": "playlist",
                        "resourceId": {"kind": "youtube#video", "videoId": "video"},
                    }
                }
            ]
        }

    clients["first"].list = pages
    assert playlists.assign("first", "video", "playlist")["state"] == "already_present"
    assert not clients["first"].mutations


def test_repeated_page_fails_closed(context):
    playlists, clients, _ = context
    original = clients["first"].list

    def pages(resource, **parameters):
        if resource == "playlistItems":
            return {"items": [], "nextPageToken": "next"}
        return original(resource, **parameters)

    clients["first"].list = pages
    with pytest.raises(ReadError, match="pagination"):
        playlists.assign("first", "video", "playlist")
    assert not clients["first"].mutations
