"""
Symmetric encryption for sensitive values stored on disk.

This module guards the Plex auth tokens written into
``server_data/servers.json`` and the legacy ``settings.json`` so a
host-disk leak (export theft, misconfigured bind mount, sloppy CI
artefact upload) does not expose the tokens to anyone with read
access to the data volume.

Key management
--------------
A single 256-bit symmetric key is generated on first boot using
``secrets.token_bytes(32)`` and written to ``server_data/.keyfile``
with ``O_EXCL | O_CREAT | O_WRONLY`` so two processes racing on first
boot don't clobber each other - exactly one creator wins; the loser
sees ``EEXIST`` and re-reads. Subsequent boots read the existing
keyfile.

If the keyfile is missing on a host that previously had encrypted
data (volume wiped, file manually deleted), :func:`_load_or_create_key`
emits a prominent WARNING and regenerates. Existing ciphertext is
unrecoverable in that case - by design.

Encryption format
-----------------
Fernet (``cryptography>=41.0``) - authenticated symmetric encryption
that bundles ciphertext, HMAC, and IV into one URL-safe base64
string. Tampered or wrong-key ciphertext raises
:class:`cryptography.fernet.InvalidToken`; callers should catch that
and produce an actionable user-facing error (see
``server_registry.decrypt_server_token``).

Threat model
------------
Encryption at rest protects against:

  * Host-disk theft or export exfiltration.
  * Bind-mount over-permissioning (another container reading
    ``server_data/`` on a shared host).
  * Container image leakage that included the data volume.

It does NOT protect against:

  * A compromised running process - once a job is running, the
    decrypted token is necessarily in memory (held by python-plexapi
    inside the ``PlexServer`` object). This is the accepted residual
    exposure documented at ``services.state._plex_token``.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
import threading
from pathlib import Path
from typing import Optional

from server.persistence import get_data_dir


log = logging.getLogger("plexmigrate.server.secrets")

# The keyfile lives inside the bind-mounted data dir so it survives
# container restarts but is deliberately co-located with the encrypted
# JSON files. If the data volume is restored from a export that
# contains both, decryption keeps working transparently.
_KEYFILE_NAME = ".keyfile"

# Lazy singleton - the Fernet instance is constructed on first use so
# this module is safe to import even when ``cryptography`` is somehow
# absent. The ImportError surfaces only when an encrypt/decrypt is
# actually attempted.
_lock = threading.Lock()
_fernet: Optional[object] = None  # cryptography.fernet.Fernet, declared lazily


def _keyfile_path() -> Path:
    return get_data_dir() / _KEYFILE_NAME


def harden_secret_file(path: Path) -> None:
    """Best-effort: lock a freshly-created secret file down to its
    owner on Windows.

    The ``0o600`` mode passed to ``os.open`` is honoured on POSIX but
    ignored by Windows, where the file inherits the data directory's
    ACL - typically granting the ``Users`` / ``Authenticated Users``
    groups read access. Strip those broad-access groups via ``icacls``
    so a secret (the Fernet keyfile, the JWT signing secret) is not
    readable by every local account on a multi-user Windows host.

    The owner / SYSTEM / Administrators ACEs are left intact, so this
    can never lock the service out of its own secret. POSIX is a
    no-op (the 0o600 mode already applied). Any failure is logged,
    never raised - a readable secret file is a hardening gap, not a
    reason to abort startup.
    """
    if os.name != "nt":
        return
    try:
        import subprocess
        # Well-known SIDs (locale-independent): BUILTIN\Users,
        # NT AUTHORITY\Authenticated Users, Everyone. /remove:g drops
        # only those groups' grants; the owner / SYSTEM /
        # Administrators ACEs are untouched.
        subprocess.run(
            ["icacls", str(path), "/remove:g",
             "*S-1-5-32-545", "*S-1-5-11", "*S-1-1-0"],
            capture_output=True, timeout=10, check=False,
        )
    except Exception:
        log.warning(
            "could not harden ACL on %s; on a multi-user Windows host "
            "the file may remain readable by other accounts", path,
        )


def _load_or_create_key() -> bytes:
    """
    Return the 32-byte raw key, creating the keyfile on first boot.

    Race-safe: ``os.open(... O_EXCL | O_CREAT | O_WRONLY)`` guarantees
    exactly one process succeeds at file creation. The loser of the
    race sees ``FileExistsError`` and falls through to a normal read.
    """
    path = _keyfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 0o600 is best-effort. Honoured on POSIX hosts; Windows
        # ignores the mode and falls back to whatever the parent
        # directory's ACL allows. The bind-mount layout already
        # restricts host-side access to whoever can read server_data/.
        #
        # O_BINARY is required on Windows: without it os.open() defaults
        # to text mode and os.write() translates every 0x0A byte in the
        # random key to 0x0D0A, producing a >32-byte keyfile that the
        # length check below then rejects. O_BINARY does not exist on
        # POSIX, where getattr falls back to 0 (a no-op).
        fd = os.open(
            str(path),
            os.O_EXCL | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        return path.read_bytes()

    try:
        key = _secrets.token_bytes(32)
        os.write(fd, key)
    finally:
        os.close(fd)

    # AUTH-07: harden the new keyfile's ACL on Windows (the 0o600
    # mode above is POSIX-only there). Best-effort - never fatal.
    harden_secret_file(path)

    # This branch is reached only on first-boot OR after a manual
    # delete of the keyfile. We can't distinguish the two reliably
    # (the directory may still hold encrypted JSON from before the
    # delete), so the warning is always emitted on creation. On a
    # genuinely fresh install the line is informational; on a
    # post-delete install it is the actionable signal the end user
    # needs to re-enter their credentials.
    log.warning(
        "Generated new encryption key at %s. If this is NOT a fresh "
        "install, any previously-stored encrypted Plex tokens are "
        "unreadable and must be re-entered under the Servers tab.",
        path,
    )
    return key


def _get_fernet() -> "object":
    """Return the singleton Fernet instance, constructing it lazily."""
    global _fernet
    if _fernet is not None:
        return _fernet
    with _lock:
        if _fernet is None:
            # Lazy import keeps this module importable on a host
            # without ``cryptography`` installed (e.g. a minimal CLI
            # checkout for read-only inspection). The ImportError
            # surfaces only at first encrypt/decrypt.
            import base64
            from cryptography.fernet import Fernet

            raw = _load_or_create_key()
            if len(raw) != 32:
                raise ValueError(
                    f"Keyfile at {_keyfile_path()!s} is the wrong length "
                    f"({len(raw)} bytes, expected 32). Delete it to "
                    f"regenerate (you will lose access to any tokens "
                    f"encrypted with the previous key)."
                )
            _fernet = Fernet(base64.urlsafe_b64encode(raw))
        return _fernet


def encrypt_str(plaintext: str) -> str:
    """
    Encrypt ``plaintext`` and return a Fernet token string suitable
    for storage in JSON. Empty input yields empty output - an empty
    token slot stays empty rather than carrying a useless ciphertext.
    """
    if not plaintext:
        return ""
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")  # type: ignore[attr-defined]


def decrypt_str(ciphertext: str) -> str:
    """
    Decrypt a Fernet token string back to plaintext. Empty input
    yields empty output.

    Raises:
        cryptography.fernet.InvalidToken - when the ciphertext is
        malformed, tampered, or was encrypted under a different key.
        Callers should catch this and emit an actionable user-facing
        message rather than letting the raw exception reach the UI.
    """
    if not ciphertext:
        return ""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")  # type: ignore[attr-defined]
