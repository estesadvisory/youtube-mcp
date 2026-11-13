"""Transport-independent, explicit channel profiles (Refs estesadvisory/portfolio-ops#285).

Profiles are trusted operator configuration, never credentials or caller-selected
channel titles. Credential material belongs behind CredentialProvider.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

YOUTUBE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"


class ProfileError(ValueError):
    """A safe, fixed-message configuration or selection failure."""


@dataclass(frozen=True, slots=True)
class Profile:
    profile_id: str
    channel_id: str
    label: str
    granted_scopes: frozenset[str]
    credential_ref: str = field(repr=False)

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in (self.profile_id, self.channel_id, self.label, self.credential_ref)
        ):
            raise ProfileError("Profile fields must be nonempty, trimmed strings.")
        if isinstance(self.granted_scopes, (str, bytes)):
            raise ProfileError("Profile scopes must be a collection of scope strings.")
        try:
            scopes = frozenset(self.granted_scopes)
        except TypeError:
            raise ProfileError("Profile scopes must be a collection of scope strings.") from None
        if any(not isinstance(scope, str) or not scope.strip() for scope in scopes):
            raise ProfileError("Profile scopes must be a collection of scope strings.")
        object.__setattr__(self, "granted_scopes", scopes)


class ProfileStore:
    """Exact profile-id selection only; labels and channel titles are display data."""

    def __init__(self, profiles: Iterable[Profile]) -> None:
        self._profiles: dict[str, Profile] = {}
        for profile in profiles:
            if profile.profile_id in self._profiles:
                raise ProfileError("Duplicate profile id.")
            self._profiles[profile.profile_id] = profile

    def resolve(self, profile_id: str | None) -> Profile:
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ProfileError("An explicit profile id is required.")
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ProfileError("Profile not found.")
        if YOUTUBE_FORCE_SSL not in profile.granted_scopes:
            raise ProfileError("Profile requires the youtube.force-ssl grant.")
        return profile


@dataclass(frozen=True, slots=True, repr=False)
class Credential:
    """Opaque provider result; never include its value in diagnostics or responses."""

    value: bytes = field(repr=False)

    def __repr__(self) -> str:
        return "Credential(<redacted>)"


class CredentialProvider(Protocol):
    """Private adapters implement this port; core profile selection does no I/O.

    Implementations must redact logs and raise fixed-message errors without
    embedding credential references, material, or upstream exception chains.
    """

    def resolve(self, credential_ref: str) -> Credential: ...


class CredentialError(ValueError):
    """Safe provider failure without credential material."""


class InMemoryCredentialProvider:
    """Offline fake only; no secret-store or Google integration."""

    def __init__(self, credentials: Mapping[str, Credential]) -> None:
        self._credentials = dict(credentials)

    def resolve(self, credential_ref: str) -> Credential:
        credential = self._credentials.get(credential_ref)
        if credential is None:
            raise CredentialError("Credential unavailable.")
        return credential
