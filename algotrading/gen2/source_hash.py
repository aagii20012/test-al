"""Platform-independent canonical hash of the Generation-2 source tree.

Algorithm name: ``python-source-canonical-sha256``  ·  version: ``2``.

WHY THIS EXISTS
---------------
The Generation-1 (v1) source binding read every ``*.py`` file as *raw bytes*.
On a Windows dev checkout those files carry CRLF (``\\r\\n``) line endings; on a
GitHub-hosted Ubuntu runner the byte-identical source is checked out with LF
(``\\n``). Raw-byte hashing therefore produced two DIFFERENT digests for the same
logical source, so an experiment bound on Windows could never re-verify its code
binding on Linux — the canary failed closed on every run (failure class
``SOURCE_BINDING_PORTABILITY``: Windows ``9681fdba…`` vs Linux ``578dfa75…``).

v2 fixes this at the root:

  * each file is decoded as **strict UTF-8** (invalid UTF-8 fails closed);
  * **all newline conventions are normalised** (CRLF and lone CR -> LF) BEFORE
    hashing, so a file's hash is identical on Windows and Linux;
  * every path and content blob is **length-prefixed** (framed) so no path or
    content boundary can be ambiguous or collide with another framing;
  * the **algorithm name and version are folded into the preimage**, so a v1
    digest can never be mistaken for a v2 digest (and vice-versa).

WHAT IT DELIBERATELY DOES *NOT* NORMALISE
-----------------------------------------
Only newlines are normalised. Spaces, tabs, indentation, Unicode form, and
comments are all significant — a real edit to any of them changes the digest.
This is a portability fix, not a semantic-equivalence fingerprint. It also does
not touch the config, strategy-source, or per-bot-params hashes, which stay
byte-exact (weakening those was explicitly out of scope).

DECLARED INVENTORY, NOT AN UNCONTROLLED GLOB
--------------------------------------------
The authoritative set of hashed files is a *committed declared inventory*
(``algotrading/gen2/source_inventory.json``), not a raw filesystem walk. The
digest is computed over exactly the files the inventory names, in
bytewise-sorted-path order. A separate **drift check** independently walks the
tree and fails closed if the set of ``*.py`` files on disk differs from the
inventory in ANY way (missing, extra, or duplicate), so neither a stray new file
nor a silently deleted one can slip through unnoticed. Every fail-closed
condition raises :class:`SourceHashError`, a subclass of
``state_schema.IncompatibleStateError`` so the coordinator's fail-closed path
treats a source-integrity violation exactly like any other incompatible state.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List, Optional, Tuple

from .. import state_schema

# --------------------------------------------------------------------------
# Algorithm identity. These two constants are part of the hash preimage AND are
# recorded in the manifest, so a manifest bound under a different algorithm or
# version can never verify against this implementation.
# --------------------------------------------------------------------------
ALGORITHM = "python-source-canonical-sha256"
VERSION = 2

# The package whose ``*.py`` files constitute the bound source tree, expressed as
# a repo-relative POSIX path. Paths in the inventory and digest are repo-relative
# (e.g. ``algotrading/gen2/experiment.py``), never package-relative.
SOURCE_ROOT_REL = "algotrading"

# Committed declared inventory, repo-relative POSIX path.
INVENTORY_REL = "algotrading/gen2/source_inventory.json"


class SourceHashError(state_schema.IncompatibleStateError):
    """Any fail-closed source-tree integrity violation.

    Subclasses ``IncompatibleStateError`` so that a missing/extra/duplicate file,
    invalid UTF-8, a symlink, a path escaping the repo root, or inventory drift
    all surface through the coordinator's existing fail-closed handling.
    """


# --------------------------------------------------------------------------
# Low-level framing helpers.
# --------------------------------------------------------------------------
def _u64(n: int) -> bytes:
    """Big-endian unsigned 64-bit length prefix."""
    if n < 0 or n > 0xFFFFFFFFFFFFFFFF:
        raise SourceHashError(f"length {n} out of range for a u64 frame")
    return int(n).to_bytes(8, "big")


def normalize_text(raw: bytes) -> bytes:
    """Strict UTF-8 decode + newline normalisation, re-encoded to UTF-8 bytes.

    CRLF and lone CR both collapse to LF. Raises ``SourceHashError`` on invalid
    UTF-8 (fail closed — never silently replace or drop bytes).
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SourceHashError(f"content is not valid UTF-8: {e}") from e
    # Order matters: collapse CRLF first, then any remaining lone CR.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.encode("utf-8")


def _repo_root() -> str:
    # .../algotrading/gen2/source_hash.py -> .../algotrading/gen2 -> .../algotrading -> repo root
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _source_root_dir(root: str) -> str:
    return os.path.join(root, *SOURCE_ROOT_REL.split("/"))


# --------------------------------------------------------------------------
# Path validation + safe resolution (fail closed on anything unexpected).
# --------------------------------------------------------------------------
def _validate_rel_path(path: str) -> None:
    """Fail closed unless ``path`` is a clean repo-relative POSIX .py path."""
    if not isinstance(path, str) or not path:
        raise SourceHashError(f"empty or non-string inventory path: {path!r}")
    if path != path.strip():
        raise SourceHashError(f"inventory path has surrounding whitespace: {path!r}")
    if "\\" in path:
        raise SourceHashError(
            f"inventory path {path!r} contains a backslash; POSIX '/' required")
    if os.path.isabs(path) or (len(path) >= 2 and path[1] == ":"):
        raise SourceHashError(f"inventory path {path!r} is absolute")
    parts = path.split("/")
    for comp in parts:
        if comp in ("", ".", ".."):
            raise SourceHashError(
                f"inventory path {path!r} has an empty or dot component")
    if parts[0] != SOURCE_ROOT_REL:
        raise SourceHashError(
            f"inventory path {path!r} is not under {SOURCE_ROOT_REL}/")
    if not path.endswith(".py"):
        raise SourceHashError(f"inventory path {path!r} is not a .py file")


def _safe_full_path(root: str, rel: str) -> str:
    """Resolve ``rel`` under ``root``, refusing symlinks and path escapes."""
    root_real = os.path.realpath(root)
    cur = root
    for comp in rel.split("/"):
        cur = os.path.join(cur, comp)
        if os.path.islink(cur):
            raise SourceHashError(
                f"inventory path {rel!r} traverses a symlink at {cur!r}")
    full = os.path.join(root, *rel.split("/"))
    if not os.path.isfile(full):
        raise SourceHashError(
            f"declared source file {rel!r} is missing at {full!r}")
    real = os.path.realpath(full)
    if real != root_real and not real.startswith(root_real + os.sep):
        raise SourceHashError(
            f"inventory path {rel!r} resolves to {real!r}, outside repo root "
            f"{root_real!r}")
    return full


# --------------------------------------------------------------------------
# Inventory (declared source set).
# --------------------------------------------------------------------------
def _walk_py(root: str) -> List[str]:
    """Every ``*.py`` under the source root, as sorted repo-relative POSIX paths."""
    src_root = _source_root_dir(root)
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        # never descend into bytecode caches
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            found.append(rel)
    return sorted(found, key=lambda p: p.encode("utf-8"))


def generate_inventory(root: Optional[str] = None) -> Dict[str, object]:
    """Build a fresh declared-inventory dict from the current source tree."""
    root = root or _repo_root()
    files = _walk_py(root)
    return {
        "algorithm": ALGORITHM,
        "version": VERSION,
        "root": SOURCE_ROOT_REL,
        "files": files,
    }


def _inventory_canonical_bytes(inv: Dict[str, object]) -> bytes:
    """Platform-independent canonical serialisation of an inventory.

    Derived from the PARSED inventory (not the raw file bytes), so the
    inventory's own hash is unaffected by the JSON file's on-disk line endings.
    """
    payload = {
        "algorithm": inv["algorithm"],
        "version": inv["version"],
        "root": inv["root"],
        "files": list(inv["files"]),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def inventory_sha256(inv: Dict[str, object]) -> str:
    return hashlib.sha256(_inventory_canonical_bytes(inv)).hexdigest()


def write_inventory(inv: Dict[str, object], path: str) -> None:
    """Write the inventory as LF-terminated, sorted-key JSON (deterministic)."""
    text = json.dumps(inv, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def load_inventory(inventory_path: Optional[str] = None, *,
                   root: Optional[str] = None) -> Dict[str, object]:
    """Load + validate the committed inventory. Fail closed on any malformity."""
    root = root or _repo_root()
    path = inventory_path or os.path.join(root, *INVENTORY_REL.split("/"))
    if not os.path.isfile(path):
        raise SourceHashError(f"source inventory not found at {path!r}")
    with open(path, "r", encoding="utf-8") as fh:
        inv = json.load(fh)
    if not isinstance(inv, dict):
        raise SourceHashError("source inventory is not a JSON object")
    # Algorithm identity: a v1 (or any other) inventory cannot masquerade as v2.
    if inv.get("algorithm") != ALGORITHM:
        raise SourceHashError(
            f"inventory algorithm {inv.get('algorithm')!r} != expected "
            f"{ALGORITHM!r}")
    if inv.get("version") != VERSION:
        raise SourceHashError(
            f"inventory version {inv.get('version')!r} != expected {VERSION!r}")
    if inv.get("root") != SOURCE_ROOT_REL:
        raise SourceHashError(
            f"inventory root {inv.get('root')!r} != expected {SOURCE_ROOT_REL!r}")
    files = inv.get("files")
    if not isinstance(files, list) or not files:
        raise SourceHashError("inventory 'files' must be a non-empty list")
    seen: set = set()
    for p in files:
        _validate_rel_path(p)
        if p in seen:
            raise SourceHashError(f"duplicate path in inventory: {p!r}")
        seen.add(p)
    return inv


def assert_no_drift(root: Optional[str] = None,
                    inv: Optional[Dict[str, object]] = None) -> None:
    """Fail closed if disk ``*.py`` set differs from the declared inventory."""
    root = root or _repo_root()
    inv = inv or load_inventory(root=root)
    declared = set(inv["files"])  # type: ignore[arg-type]
    found = set(_walk_py(root))
    missing = declared - found
    extra = found - declared
    if missing or extra:
        raise SourceHashError(
            "source inventory drift detected: "
            f"missing={sorted(missing)} extra={sorted(extra)}. The declared "
            "inventory must exactly match the *.py files on disk.")


# --------------------------------------------------------------------------
# The canonical digest.
# --------------------------------------------------------------------------
def _digest(entries: List[Tuple[bytes, bytes]]) -> str:
    """Framed SHA-256 over (algorithm, version, count, [path, content]...)."""
    h = hashlib.sha256()
    name = ALGORITHM.encode("utf-8")
    h.update(_u64(len(name)))
    h.update(name)
    h.update(_u64(VERSION))
    h.update(_u64(len(entries)))
    for path_bytes, content in entries:
        h.update(_u64(len(path_bytes)))
        h.update(path_bytes)
        h.update(_u64(len(content)))
        h.update(content)
    return h.hexdigest()


def canonical_source_hash(root: Optional[str] = None,
                          inventory_path: Optional[str] = None,
                          *, drift_check: bool = True) -> Dict[str, object]:
    """Compute the platform-independent v2 source-tree hash.

    Returns a dict carrying ``algorithm``, ``version``, ``sha256``,
    ``file_count`` and ``inventory_sha256``. The ``sha256`` + ``file_count`` keys
    are preserved from the v1 return shape so existing callers/monkeypatches keep
    working; the extra keys let the manifest record (and the coordinator verify)
    the algorithm identity and the exact declared inventory.
    """
    root = root or _repo_root()
    inv = load_inventory(inventory_path, root=root)
    if drift_check:
        assert_no_drift(root, inv)

    files = sorted(inv["files"], key=lambda p: p.encode("utf-8"))  # type: ignore[arg-type]
    entries: List[Tuple[bytes, bytes]] = []
    for rel in files:
        full = _safe_full_path(root, rel)
        with open(full, "rb") as fh:
            content = normalize_text(fh.read())
        entries.append((rel.encode("utf-8"), content))

    return {
        "algorithm": ALGORITHM,
        "version": VERSION,
        "sha256": _digest(entries),
        "file_count": len(entries),
        "inventory_sha256": inventory_sha256(inv),
    }
