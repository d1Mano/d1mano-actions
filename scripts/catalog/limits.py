"""Caps and constants for ingestion, each one derived from a measured maximum.

Every cap below was re-derived by measuring the real corpus rather than picked for
roundness. The headroom column is the ratio between the cap and the largest member
of the corpus folder; it is written down so that a future edit can tell "I moved the cap
because I measured more" from "I moved the cap because a file annoyed me".

The 32 MB uncompressed cap that appeared in the original proposal is absent on
purpose. The largest of the six corpus archives declares 37.93 MB and is a
known-good file, so that number rejected healthy input.

No vendor name, folder name or filename appears in this module, and neither does one
appear anywhere else in the package. These files are destined for the public repo, and
the names in the corpus are this customer's suppliers (R-003, R-019 level 4). The
measurements stay because they are what a reader needs in order to judge whether a
cap still fits; the identities do not, because nobody outside the business needs them.
"""

# Measured maxima over the six archives in the corpus:
#   declared uncompressed  37.93 MB  (the largest archive)
#   archive entries          266
#   the selected .txt        58 344 bytes  (the largest chat)
MAX_ARCHIVE_ENTRIES = 2000        # 7.5x headroom
MAX_DECLARED_BYTES = 64 * 1024 * 1024   # 64 MiB, 1.7x headroom
MAX_CHAT_BYTES = 2 * 1024 * 1024        # 34x headroom over the largest .txt

# The declared size in a zip's central directory is attacker-controlled and need
# not match what the member actually inflates to, so the member is also copied out
# through a counting reader. This is the chunk size for that copy, not a policy.
COPY_CHUNK_BYTES = 64 * 1024

# A bare .txt upload carries no entry count and no declared total, so only the
# on-disk size and the same per-chat cap apply.
MAX_TXT_UPLOAD_BYTES = MAX_CHAT_BYTES

# R-021/G-14. Chat timestamps are local wall clock with no zone and
# ``runs.effective_date`` is ``timestamptz``, so the boundary between "counts" and
# "too old" is a real population of products rather than an edge case. The zone is
# a per-run value; this is only the seed used when a caller does not supply one.
# It is a constant in code and not a column default precisely because R-036 freezes
# the run: correcting the zone is a relaunch, not a migration on a table that
# already holds live runs.
DEFAULT_EFFECTIVE_TZ = "America/Havana"

# WhatsApp writes two-digit years. Everything in the corpus is 26, but a parser
# that hardcodes 2026 stops being a parser. The pivot is the usual one: values at
# or below this bound are 2000s, the rest are 1900s.
YEAR_PIVOT = 68
YEAR_CENTURY = 2000
YEAR_PREV_CENTURY = 1900

# ``run_items.item_key`` is CHECKed against ^[A-Za-z0-9_-]+-[0-9]{3,6}$, so the
# ordinal is at least three digits and the separator is unambiguous inside a
# provider key that may itself contain underscores.
ORDINAL_MIN_DIGITS = 3
ORDINAL_MAX_DIGITS = 6