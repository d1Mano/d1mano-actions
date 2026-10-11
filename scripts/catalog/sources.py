"""Locating the chat inside an uploaded source (R-009, G-03).

A source arrives as either a ``.zip`` holding one WhatsApp export among hundreds of
photos, or as a bare ``.txt``. There is no zip handling anywhere in this repo's
history: the legacy script probes two hardcoded filenames and then globs
``Chat*.txt`` inside a directory. Five of the six corpus vendors name their export one
way and one names it another, which is exactly why a hardcoded name cannot work, and
globbing is worse -- it silently picks a winner when the owner meant to hand over one
specific file.

Two properties are non-negotiable here, and they are different properties:

**No traversal path is ever constructed.** The member's own name is never a path
component. The bytes land on a path this module invents, under a directory it was
given, from a slot number that came from the run's own source list. A member called
``../../etc/passwd`` is therefore not a rejected input -- it is an input whose name is
never consulted, which is stronger than a check that could itself be wrong.

**The declared size is the thing to police, not the received one.** A zip bomb is a
lie in the central directory: the file on disk is small, the declared uncompressed
total is enormous. So entry count and declared totals are checked *before* a single
byte is inflated. The copy then runs through a counting reader anyway, because the
declaration can lie in the other direction too.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import limits

CHAT_SUFFIX = ".txt"
ARCHIVE_SUFFIXES = (".zip",)


class SourceRejected(Exception):
    """A source cannot be ingested. The message is the diagnostic.

    Raised before any database interaction: ingestion is the only stage in the whole
    pipeline that runs with no credentials (§7.1), so this string is the only thing
    a failing archive leaves behind.
    """


@dataclass(frozen=True)
class ChatFile:
    """Where the chat landed, and why we believe it is the right one.

    ``origin`` records how the bytes got here rather than trusting the caller's
    bookkeeping: an operator debugging "why did this source produce 0 products"
    needs to know whether the file was read as an archive or as plain text.
    """

    path: Path
    declared_bytes: int
    entry_count: int
    origin: str


class _CountingReader:
    """Wraps a member stream and refuses to hand out more bytes than the cap.

    This is depth, not the primary guard. Measured: when a zip's headers understate a
    member's uncompressed size, CPython's ``ZipExtFile`` stops at the declared size and
    fails its CRC check having written zero bytes -- so the stdlib already covers the
    understatement vector on its own. The reader is here because the cap is a policy
    this pipeline states, and a policy that depends on the internals of the reader
    underneath it is not one.
    """

    def __init__(self, stream, cap: int) -> None:
        self._stream = stream
        self._cap = cap
        self._seen = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self._seen += len(chunk)
        if self._seen > self._cap:
            raise SourceRejected(
                f"member inflated past the {self._cap} byte chat cap "
                f"(declared size understated); nothing else was extracted"
            )
        return chunk


def _is_chat(name: str) -> bool:
    # Case-insensitive because an export saved from a phone can arrive as ".TXT",
    # and refusing it would send the owner looking for a bug that is not one.
    return name.lower().endswith(CHAT_SUFFIX)


def select_chat_member(archive: ZipFile) -> zipfile.ZipInfo:
    """Return the archive's single ``.txt`` member, or explain why there isn't one.

    Two ``.txt`` members is a refusal, not a coin flip. Picking the longer one, or
    the lexicographically first, would ingest a file the owner did not mean to
    upload and produce a plausible-looking catalog from the wrong chat -- the worst
    possible failure mode, because it is silent.
    """
    members = [info for info in archive.infolist() if _is_chat(info.filename)]
    if not members:
        raise SourceRejected(
            f"archive has no {CHAT_SUFFIX} member among {len(archive.infolist())} entries; "
            "a WhatsApp export must be uploaded with its .txt"
        )
    if len(members) > 1:
        names = sorted(info.filename for info in members)
        raise SourceRejected(
            "archive holds "
            f"{len(members)} {CHAT_SUFFIX} members ({', '.join(names)}); refusing to guess "
            "which chat was meant -- upload only the intended export"
        )
    return members[0]


MIB = 1024 * 1024


def _check_declared(archive: ZipFile) -> tuple[int, int]:
    """Enforce the caps against the central directory, before inflating anything.

    Both sides of each comparison are reported in MiB. The cap is written as
    ``64 * 1024 * 1024``, and printing it as "67 MB" against a declared total printed
    as "83.9 MB" invites a decimal/binary mismatch -- which is precisely the class of
    error that let the proposal's 32 MB cap reject a healthy corpus file for a year
    before anyone noticed.
    """
    infos = archive.infolist()
    if len(infos) > limits.MAX_ARCHIVE_ENTRIES:
        raise SourceRejected(
            f"archive declares {len(infos)} entries, over the {limits.MAX_ARCHIVE_ENTRIES} cap"
        )
    declared = sum(info.file_size for info in infos)
    if declared > limits.MAX_DECLARED_BYTES:
        raise SourceRejected(
            f"archive declares {declared / MIB:.1f} MiB uncompressed, over the "
            f"{limits.MAX_DECLARED_BYTES / MIB:.0f} MiB cap"
        )
    return len(infos), declared


def _chat_slot_dir(work_dir: Path, slot: int, label: str) -> Path:
    """Create the destination for one source. The path comes from ``slot`` alone.

    ``slot`` is the source's position in the run's own source list, and ``label`` is
    a cosmetic suffix validated against a strict allowlist. Neither is read from the
    archive, and neither is the member name, so there is no component here that an
    attacker controls.
    """
    safe_label = "".join(ch for ch in label if ch.isascii() and (ch.isalnum() or ch in "-_"))
    directory = work_dir / f"slot-{slot:02d}" / (safe_label or "chat")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_stream(stream, destination: Path, cap: int) -> int:
    """Copy ``stream`` to ``destination`` through the counting reader."""
    written = 0
    reader = _CountingReader(stream, cap)
    with destination.open("wb") as out:
        while True:
            chunk = reader.read(limits.COPY_CHUNK_BYTES)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
    return written


def _read_archive(source: Path, destination: Path) -> tuple[int, int]:
    # A damaged archive is an expected upload failure, not a bug: truncated
    # transfers and rewrites on the phone both land here. Without this, zipfile's
    # BadZipFile escapes as a traceback and the operator gets a stack instead of the
    # one line that would have told them what to re-send.
    try:
        with zipfile.ZipFile(source) as archive:
            entries, _declared = _check_declared(archive)
            member = select_chat_member(archive)
            if member.file_size > limits.MAX_CHAT_BYTES:
                raise SourceRejected(
                    f"chat member declares {member.file_size / 1024:.0f} KB, over the "
                    f"{limits.MAX_CHAT_BYTES / 1024:.0f} KB per-chat cap"
                )
            written = _write_stream(archive.open(member), destination, limits.MAX_CHAT_BYTES)
    except zipfile.BadZipFile as exc:
        raise SourceRejected(f"archive is not readable as a zip ({exc})") from exc
    return written, entries


def _read_plain(source: Path, destination: Path) -> tuple[int, int]:
    size = source.stat().st_size
    if size > limits.MAX_TXT_UPLOAD_BYTES:
        raise SourceRejected(
            f"uploaded {CHAT_SUFFIX} is {size / 1024:.0f} KB, over the "
            f"{limits.MAX_TXT_UPLOAD_BYTES / 1024:.0f} KB cap"
        )
    with source.open("rb") as stream:
        written = _write_stream(stream, destination, limits.MAX_CHAT_BYTES)
    return written, 1


def materialize_chat(source: Path, slot: int, work_dir: Path, label: str = "") -> ChatFile:
    """Put one source's chat on disk at a path this module chose, and return it.

    ``slot`` numbers the sources in the order the run declared them. That order is
    frozen by R-036, which is what lets the destination path be reproducible: the
    same run re-derived in a different process writes to the same path.
    """
    if not source.is_file():
        raise SourceRejected(f"source is not a readable file: {source.name}")

    suffix = source.suffix.lower()
    directory = _chat_slot_dir(work_dir, slot, label)
    destination = directory / f"chat{CHAT_SUFFIX}"

    if suffix in ARCHIVE_SUFFIXES:
        origin = "zip"
        reader = _read_archive
    elif suffix == CHAT_SUFFIX:
        origin = "txt"
        reader = _read_plain
    else:
        raise SourceRejected(
            f"source ends in {suffix or 'nothing'}; R-009 accepts "
            f"{', '.join(ARCHIVE_SUFFIXES)} and {CHAT_SUFFIX} only"
        )

    try:
        written, entries = reader(source, destination)
    except SourceRejected:
        # Half a chat on disk is worse than none: a later slice that finds the file
        # would ingest a truncated export and produce a plausible catalog from it.
        destination.unlink(missing_ok=True)
        raise

    # Belt and braces on the property the design claims by construction. If the
    # destination is ever built from something other than the slot number, this is
    # the line that notices, and it notices after the write rather than before --
    # which is why the construction above is the real control.
    resolved = destination.resolve()
    if work_dir.resolve() not in resolved.parents:
        raise SourceRejected(f"chat landed outside the work directory: {resolved}")

    return ChatFile(
        path=destination,
        declared_bytes=written,
        entry_count=entries,
        origin=origin,
    )