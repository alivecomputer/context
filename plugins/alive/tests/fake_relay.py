#!/usr/bin/env python3
"""In-memory FakeRelay for P2P round-trip tests (LD25 / fn-7-7cw.11).

Mirrors the LD25 GitHub relay wire protocol semantics without git, network,
or process spawning. The blob store uses ``(owner, peer, filename)`` keys to
match the on-disk path convention ``inbox/<sender>/<package>.walnut``.

Operations exposed:

    upload(owner, peer, filename, data)        -- deposit a blob
    download(owner, peer, filename) -> bytes   -- fetch a blob
    list_pending(owner, peer)                  -- list filenames in inbox
    delete(owner, peer, filename)              -- remove a blob
    register_peer(owner, peer, pubkey_pem)     -- record a peer's public key

The contract intentionally takes BOTH the relay owner AND the source peer
because each owner-side relay holds an inbox per sender. Tests that only
care about a single owner can pass a constant for ``owner``.

Stdlib only. No filesystem, no subprocess, no network.
"""

from typing import Dict, List, Optional, Tuple


class FakeRelayError(Exception):
    """Raised on relay-side failures (missing blob, missing peer key)."""


class FakeRelay(object):
    """In-process relay abstraction.

    Storage layout:
        ``self._blobs``  -- ``{(owner, peer, filename): bytes}``
        ``self._peers``  -- ``{owner: {peer_name: pubkey_pem_bytes}}``

    Methods are deterministic and safe for repeated test runs because each
    test instantiates a fresh ``FakeRelay()``.
    """

    def __init__(self):
        # type: () -> None
        self._blobs = {}  # type: Dict[Tuple[str, str, str], bytes]
        self._peers = {}  # type: Dict[str, Dict[str, bytes]]

    # ------------------------------------------------------------------ blobs

    def upload(self, owner, peer, filename, data):
        # type: (str, str, str, bytes) -> str
        """Deposit a blob at ``inbox/<peer>/<filename>`` of ``owner``'s relay.

        Returns the canonical blob path string for diagnostics.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                "FakeRelay.upload: data must be bytes, got {0}".format(
                    type(data).__name__
                )
            )
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise ValueError(
                "FakeRelay.upload: invalid filename {0!r}".format(filename)
            )
        self._blobs[(owner, peer, filename)] = bytes(data)
        return "inbox/{0}/{1}".format(peer, filename)

    def download(self, owner, peer, filename):
        # type: (str, str, str) -> bytes
        """Fetch a blob. Raises FakeRelayError when missing."""
        key = (owner, peer, filename)
        if key not in self._blobs:
            raise FakeRelayError(
                "FakeRelay.download: no blob for owner={0} peer={1} "
                "filename={2}".format(owner, peer, filename)
            )
        return self._blobs[key]

    def list_pending(self, owner, peer=None):
        # type: (str, Optional[str]) -> List[str]
        """List filenames currently held in ``owner``'s relay.

        When ``peer`` is None, returns every blob across senders. Otherwise
        only blobs deposited by ``peer`` are listed. Sorted for stability.
        """
        result = []  # type: List[str]
        for (b_owner, b_peer, b_filename), _ in self._blobs.items():
            if b_owner != owner:
                continue
            if peer is not None and b_peer != peer:
                continue
            result.append(b_filename)
        return sorted(result)

    def delete(self, owner, peer, filename):
        # type: (str, str, str) -> None
        """Remove a blob. Raises FakeRelayError when missing."""
        key = (owner, peer, filename)
        if key not in self._blobs:
            raise FakeRelayError(
                "FakeRelay.delete: no blob for owner={0} peer={1} "
                "filename={2}".format(owner, peer, filename)
            )
        del self._blobs[key]

    # ------------------------------------------------------------------ peers

    def register_peer(self, owner, peer, pubkey_pem):
        # type: (str, str, bytes) -> None
        """Record a peer's public key under ``owner``'s relay."""
        if not isinstance(pubkey_pem, (bytes, bytearray)):
            raise TypeError(
                "FakeRelay.register_peer: pubkey_pem must be bytes"
            )
        owner_keys = self._peers.setdefault(owner, {})
        owner_keys[peer] = bytes(pubkey_pem)

    def get_peer_pubkey(self, owner, peer):
        # type: (str, str) -> bytes
        """Return a peer's stored public key. Raises FakeRelayError if absent."""
        if owner not in self._peers or peer not in self._peers[owner]:
            raise FakeRelayError(
                "FakeRelay.get_peer_pubkey: no key for owner={0} peer={1}".format(
                    owner, peer
                )
            )
        return self._peers[owner][peer]

    def has_peer(self, owner, peer):
        # type: (str, str) -> bool
        return owner in self._peers and peer in self._peers[owner]

    # ----------------------------------------------------------------- intro

    def __repr__(self):
        # type: () -> str
        return "FakeRelay(blobs={0}, owners={1})".format(
            len(self._blobs), len(self._peers),
        )


__all__ = ["FakeRelay", "FakeRelayError"]
