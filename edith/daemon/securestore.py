"""SecureStore — the encrypted-at-rest data directory (spec 01 §Startup step 3).

In production the Memory store lives on a dedicated encrypted APFS volume that
edithd mounts on start (using the Keychain key) and unmounts on kill. That is a
real macOS ``hdiutil``/``diskutil`` operation and MUST NOT run in tests or this
build. So it lives behind this interface.

The dev implementation (``LocalSecureStore``) does the honest, testable subset:
it ensures the data directory exists with **0700** perms (owner-only), matching
the ``.env.example`` dev note ("a local path with 0700 perms"). It performs no
mount and holds no key.

# TODO(encrypted-volume): a production ``EncryptedVolumeStore`` implements
# ``open()`` as "mount the encrypted APFS volume with the Keychain key" and
# ``close()`` as "unmount + zero the key". Wire it in when that slice lands;
# EdithDaemon depends only on this Protocol, so the swap is transparent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

_DIR_MODE = 0o700


class SecureStore(Protocol):
    """The at-rest storage seam edithd opens on start / closes on kill."""

    @property
    def data_dir(self) -> Path: ...

    def open(self) -> None: ...

    def close(self) -> None: ...


class LocalSecureStore:
    """Dev SecureStore: enforce a 0700 data dir. No mount, no key (see module TODO)."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def open(self) -> None:
        """Ensure the data dir exists with owner-only (0700) perms."""
        self._data_dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        # mkdir's mode is masked by umask on create and is a no-op if the dir
        # already existed, so set it explicitly to guarantee 0700 either way.
        os.chmod(self._data_dir, _DIR_MODE)

    def close(self) -> None:
        """No-op for the local store; the encrypted-volume impl unmounts here."""
        return
