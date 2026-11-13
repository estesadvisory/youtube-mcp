"""Profile acceptance tests; entirely offline, with synthetic credentials."""

from dataclasses import fields

import pytest

from youtube_mcp.profiles import (
    YOUTUBE_FORCE_SSL,
    Credential,
    CredentialError,
    CredentialProvider,
    InMemoryCredentialProvider,
    Profile,
    ProfileError,
    ProfileStore,
)


def profile(**overrides):
    values = dict(
        profile_id="primary",
        channel_id="UC-pinned",
        label="Channel title",
        granted_scopes=frozenset({YOUTUBE_FORCE_SSL}),
        credential_ref="opaque-ref",
    )
    return Profile(**(values | overrides))


def test_record_and_explicit_resolution():
    record = profile()
    assert {field.name for field in fields(record)} == {
        "profile_id",
        "channel_id",
        "label",
        "granted_scopes",
        "credential_ref",
    }
    assert ProfileStore([record]).resolve("primary") is record
    assert record.channel_id == "UC-pinned"
    assert record.credential_ref == "opaque-ref"
    # Frozen slotted dataclasses reject new attributes as TypeError on Python 3.11.
    with pytest.raises((AttributeError, TypeError)):
        record.access_token = "forbidden"


@pytest.mark.parametrize("selection", [None, "", " ", "unknown", "Channel title", " primary"])
def test_missing_title_and_silent_default_fail_closed(selection):
    with pytest.raises(ProfileError):
        ProfileStore([profile()]).resolve(selection)


def test_missing_required_scope_and_upload_only_fail_closed():
    for scopes in [frozenset(), frozenset({"https://www.googleapis.com/auth/youtube.upload"})]:
        with pytest.raises(ProfileError, match="youtube.force-ssl"):
            ProfileStore([profile(granted_scopes=scopes)]).resolve("primary")


def test_exact_id_pins_channel_even_with_duplicate_labels():
    first = profile()
    second = profile(profile_id="other", channel_id="UC-other")
    store = ProfileStore([first, second])
    assert store.resolve("other").channel_id == "UC-other"
    with pytest.raises(ProfileError):
        store.resolve(first.label)
    with pytest.raises(ProfileError, match="Duplicate"):
        ProfileStore([first, first])


def test_provider_interface_fake_and_safe_diagnostics(caplog):
    material = b"synthetic-private-material"
    reference = "synthetic-private-reference"
    credential = Credential(material)
    provider: CredentialProvider = InMemoryCredentialProvider({reference: credential})
    assert provider.resolve(reference) is credential
    record = profile(credential_ref=reference)
    with pytest.raises(CredentialError) as failure:
        provider.resolve(material.decode())
    with pytest.raises(ProfileError) as selection_failure:
        ProfileStore([record]).resolve(material.decode())
    diagnostics = repr(credential) + repr(record) + str(failure.value)
    diagnostics += str(selection_failure.value) + caplog.text
    assert material.decode() not in diagnostics
    assert reference not in diagnostics
    assert failure.value.__context__ is None


@pytest.mark.parametrize("name", ["profile_id", "channel_id", "label", "credential_ref"])
@pytest.mark.parametrize("value", ["", " ", " padded ", None])
def test_invalid_profile_fields(name, value):
    with pytest.raises(ProfileError):
        profile(**{name: value})


@pytest.mark.parametrize("scopes", ["scope", [None], [["unhashable"]], None])
def test_invalid_scopes(scopes):
    with pytest.raises(ProfileError):
        profile(granted_scopes=scopes)


def test_scopes_snapshot_cannot_be_mutated_by_config_owner():
    scopes = {YOUTUBE_FORCE_SSL}
    record = profile(granted_scopes=scopes)
    scopes.clear()
    assert ProfileStore([record]).resolve("primary") is record


@pytest.mark.parametrize("selection", [None, "", "unknown", "Channel title"])
def test_coexisting_profiles_keep_channels_and_credentials_isolated(selection):
    # Refs estesadvisory/portfolio-ops#285: multiple accounts coexist without defaults.
    first = profile(credential_ref="opaque-first")
    second = profile(profile_id="other", channel_id="UC-other", credential_ref="opaque-second")
    store = ProfileStore([first, second])
    first_credential = Credential(b"synthetic-first")
    second_credential = Credential(b"synthetic-second")
    provider: CredentialProvider = InMemoryCredentialProvider(
        {"opaque-first": first_credential, "opaque-second": second_credential}
    )
    for name, channel, reference, credential in [
        ("primary", "UC-pinned", "opaque-first", first_credential),
        ("other", "UC-other", "opaque-second", second_credential),
        ("primary", "UC-pinned", "opaque-first", first_credential),
    ]:
        resolved = store.resolve(name)
        assert (resolved.channel_id, resolved.credential_ref) == (channel, reference)
        assert provider.resolve(resolved.credential_ref) is credential
    with pytest.raises(ProfileError):
        store.resolve(selection)
    # A selected account's missing credential must not fall back to a working account.
    incomplete_provider = InMemoryCredentialProvider({"opaque-first": first_credential})
    with pytest.raises(CredentialError):
        incomplete_provider.resolve(store.resolve("other").credential_ref)
    assert incomplete_provider.resolve(store.resolve("primary").credential_ref) is first_credential


def test_invalid_selected_profile_does_not_fall_back_to_another_account():
    store = ProfileStore([profile(), profile(profile_id="other", granted_scopes=frozenset())])
    with pytest.raises(ProfileError, match="youtube.force-ssl"):
        store.resolve("other")
    assert store.resolve("primary").channel_id == "UC-pinned"
