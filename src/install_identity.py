"""The stable, opaque identity of one MINIMAL install, held outside both databases.

A row that leaves this machine has to say which install produced it. Until now nothing
did: two installs syncing into one Turso database produced rows that were not merely
hard to tell apart, they were identical in every column that a reader could look at.

WHY THIS DOES NOT LIVE IN A DATABASE, which is the whole design:

`claude_usage.db` is not a local file when the tracker is configured for Turso. With
`TURSO_DATABASE_URL` set, `src/database.py:23-30` opens it through
`libsql.connect(..., sync_url=...)`, which makes it an embedded REPLICA, and
`src/database.py:33-39` calls `conn.sync()` on it. Sync is not a one-way push. A second
install pointed at the same Turso database PULLS the first install's rows down into its
own file. An install id stored in that database would therefore arrive on the second
machine, and both machines would then read the same id and claim one identity - the
exact confusion this module exists to remove.

`~/.usage-tracker/activity.db` has no sync path today, but it is not a home for the id
either. Its location is redirectable per-process by `USAGE_TRACKER_ACTIVITY_DB`
(`src/focus.py:72-76`), so an identity keyed to it would change whenever the data file
moved. And the id has to be IDENTICAL in both databases, which means it cannot be owned
by either one: one of them would be reading the other's copy.

So the id lives in its own small file and both databases are stamped from it.

The file holds an IDENTIFIER, not a credential. It grants nothing, authenticates
nothing, and is safe to read; the standing rule that credential values are never written
to files is about secrets and is not softened here. The machine token that the fleet
receiver will issue is a credential and belongs in the OS keychain, not here.

ABSENT IS ABSENT. A file that exists but does not hold a well-formed id is left exactly
where it is and reported as unknown. Regenerating on the spot would mint a brand new
identity out of a transient read failure and then write it onto rows as though it had
been observed - a silent, unauditable claim. Rows written while the id is unknown carry
`unknown`, which is a reserved marker that cannot collide with a real id because a real
id is 32 lowercase hex characters.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# What a row carries when this install's identity was never observed. It is deliberately
# not id-shaped: `is_known` only accepts 32 lowercase hex characters, so no reader can
# mistake this marker for an install that actually exists.
UNKNOWN_INSTALL_ID = "unknown"

# `uuid.uuid4().hex` and nothing else. uuid1 embeds the MAC address, and a hostname or
# an OS username collides across machines and changes under the user's feet.
_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

_cached_ids: dict[str, str] = {}
_cache_lock = threading.Lock()


def id_file_path() -> Path:
    """Where the install id is persisted."""
    configured = os.environ.get("USAGE_TRACKER_INSTALL_ID_FILE")
    # Truthiness on a PATH is fine: a path is configuration, and an empty string is not
    # a location. The standing rule is about values carrying identity, provenance or
    # measurement, and the id VALUE itself is never tested this way - see `is_known`.
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".usage-tracker" / "install_id"


def is_known(value: object) -> bool:
    """True only for a well-formed install id.

    Shape, never truth. `''`, `'0'` and `0` are all falsy and none of them mean absent;
    `'unknown'` is truthy and DOES mean absent. Only the pattern separates them.
    """
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def current() -> str | None:
    """This install's id, or None when it is genuinely unknown.

    Mints and persists one on first call. Never raises: an install that cannot write its
    id still has to start, because the free Lite tier's promise is zero-config startup.
    """
    path = id_file_path()
    key = str(path)
    with _cache_lock:
        cached = _cached_ids.get(key)
        if cached is not None:
            return cached

        existing = _read(path)
        if existing is not None:
            _cached_ids[key] = existing
            return existing
        if path.exists():
            # It may simply have APPEARED between the read above and this check: another
            # process minting at the same moment publishes it atomically, so a file that
            # exists now is either complete or genuinely malformed. One more read tells
            # the two apart. Without this, a cold start with the API and the collector
            # racing marks the loser's rows `unknown` even though the id was fine.
            existing = _read(path)
            if existing is not None:
                _cached_ids[key] = existing
                return existing
            # Present and malformed. Left untouched on purpose: overwriting it would
            # destroy the only evidence of what went wrong, and would hand this machine
            # a second identity for the same install.
            logger.warning(
                "install id file %s does not contain a well-formed id; "
                "rows will be stamped %r until it is repaired or removed",
                path,
                UNKNOWN_INSTALL_ID,
            )
            return None

        minted = _mint(path)
        if minted is None:
            return None
        _cached_ids[key] = minted
        return minted


def stamp() -> str:
    """The value to write into a row's install column.

    `value is None`, never `value or UNKNOWN_INSTALL_ID`: the fallback operator would
    also fire on `''` and `'0'`, which are values, not absences.
    """
    value = current()
    if value is None:
        return UNKNOWN_INSTALL_ID
    return value


def reset_cache() -> None:
    """Forget the memoised id. For tests that move the file between cases."""
    with _cache_lock:
        _cached_ids.clear()


def _read(path: Path) -> str | None:
    """The id the file holds, or None if it holds anything else."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("could not read install id file %s: %s", path, exc)
        return None
    if is_known(text):
        return text
    return None


# How long a loser of the mint race will wait for the winner's file to become
# readable, and how often it looks. The wait exists only for the pathological case
# where the winner is descheduled between publishing and its own read; the publish
# itself is atomic, so in practice the first look succeeds.
_SETTLE_TIMEOUT_SECONDS = 2.0
_SETTLE_INTERVAL_SECONDS = 0.01


def _mint(path: Path) -> str | None:
    """Create the id file exactly once, or read whoever created it first.

    PUBLISHED WITH ITS CONTENT ALREADY IN IT. The obvious version - O_CREAT|O_EXCL and
    then write - creates the file and fills it in two steps, and between those two steps
    the file EXISTS AND IS EMPTY. On a fresh install launchd starts the API and the
    collector together, so the loser of that race opens a real file, reads nothing,
    finds no well-formed id and stamps `unknown` onto rows that a moment later would
    have carried the real id. It self-heals on the next call, but those rows stay marked
    unknown forever. Measured, not theorised: a spin-reading second process observed the
    empty window in 7 of 40 cold starts.

    So the content is written to a temp file first and `os.link` publishes it. link is
    atomic and fails with FileExistsError if the name is taken, so it keeps the O_EXCL
    property that decides the winner, while the name it publishes has never existed in
    an unwritten state. Deliberately NOT `os.replace`: rename would let a loser clobber
    a winner that has already stamped rows with its own id.
    """
    candidate = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        won = _publish(path, candidate)
    except OSError as exc:
        logger.warning(
            "could not create install id file %s: %s; this install has no identity yet",
            path,
            exc,
        )
        return None
    if won:
        return candidate
    return _settled(path)


def _publish(path: Path, candidate: str) -> bool:
    """Link a fully-written temp file into place. True if we won the race."""
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".install_id.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(candidate + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        try:
            os.link(temp_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        # The temp name is always removed: on a win the id now has its real name, and
        # on a loss the candidate is discarded unused.
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _settled(path: Path) -> str | None:
    """The winner's id, waiting briefly for it to become readable."""
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    while True:
        value = _read(path)
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            logger.warning(
                "another process holds install id file %s but it never became readable",
                path,
            )
            return None
        time.sleep(_SETTLE_INTERVAL_SECONDS)
