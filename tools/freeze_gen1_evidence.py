"""Freeze Generation 1 paper-trade state as immutable forensic evidence.

Generation 1 (the invalidated crypto paper bake-off) must be preserved exactly
as it was recorded so its results remain auditable AFTER the corrected code and a
fresh Generation 2 run exist. This tool is *read-only* with respect to the state
files: it never opens them for writing, never moves or deletes them. It:

  1. reads every ``state/*.json`` in byte-exact mode,
  2. computes each file's SHA-256 and size,
  3. copies each file verbatim into ``evidence/gen1/state_snapshot/`` (a frozen
     duplicate that survives any later reset of the live ``state/`` directory),
  4. verifies the copy hashes identically to the original,
  5. writes ``evidence/gen1/MANIFEST.json`` (machine-readable) and
     ``evidence/gen1/MANIFEST.sha256`` (human/tool-checkable), including a hash
     of the manifest's own file list so tampering is detectable.

Run:  python -m tools.freeze_gen1_evidence
It is idempotent: re-running re-verifies existing copies and refuses to continue
if a previously frozen copy no longer matches its recorded hash (tamper alarm).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "state"
EVIDENCE_DIR = REPO / "evidence" / "gen1"
SNAPSHOT_DIR = EVIDENCE_DIR / "state_snapshot"

# The identifier stamped on the invalidated run. Every artifact carries it so no
# Gen1 file can ever be mistaken for a fresh Generation 2 state.
GENERATION = "gen1"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:            # binary: hash the exact bytes on disk
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def freeze() -> dict:
    if not STATE_DIR.is_dir():
        raise SystemExit(f"no state directory at {STATE_DIR}")

    originals = sorted(STATE_DIR.glob("*.json"))
    if not originals:
        raise SystemExit(f"no state/*.json files to freeze in {STATE_DIR}")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    entries = []
    for src in originals:
        digest = sha256_of(src)
        size = src.stat().st_size
        copy = SNAPSHOT_DIR / src.name

        if copy.exists():
            # Tamper check: a previously frozen copy must still match the source.
            existing = sha256_of(copy)
            if existing != digest:
                raise SystemExit(
                    f"TAMPER ALARM: frozen copy {copy} (sha256 {existing}) no "
                    f"longer matches source {src} (sha256 {digest}). Refusing to "
                    "overwrite evidence. Investigate before proceeding.")
        else:
            shutil.copy2(src, copy)        # verbatim copy, preserves mtime
            if sha256_of(copy) != digest:
                raise SystemExit(f"copy of {src} did not hash-match after write")

        entries.append({
            "file": src.name,
            "source_path": str(src.relative_to(REPO)).replace("\\", "/"),
            "snapshot_path": str(copy.relative_to(REPO)).replace("\\", "/"),
            "size_bytes": size,
            "sha256": digest,
            "source_mtime_utc": datetime.fromtimestamp(
                src.stat().st_mtime, tz=timezone.utc).isoformat(),
        })

    # A hash over the sorted (file, sha256) list detects any add/remove/alter of
    # the set as a whole, independent of the individual file hashes.
    set_digest = hashlib.sha256(
        "\n".join(f"{e['file']}:{e['sha256']}" for e in entries).encode()
    ).hexdigest()

    manifest = {
        "generation": GENERATION,
        "status": "INVALIDATED",
        "purpose": "Immutable forensic evidence of the invalidated Generation 1 "
                   "paper bake-off. Do not modify or delete.",
        "frozen_utc": datetime.now(timezone.utc).isoformat(),
        "state_dir": str(STATE_DIR.relative_to(REPO)).replace("\\", "/"),
        "file_count": len(entries),
        "set_sha256": set_digest,
        "hash_algorithm": "sha256",
        "files": entries,
    }

    (EVIDENCE_DIR / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    lines = [f"{e['sha256']}  {e['file']}" for e in entries]
    lines.append(f"# set_sha256: {set_digest}")
    lines.append(f"# frozen_utc: {manifest['frozen_utc']}")
    (EVIDENCE_DIR / "MANIFEST.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")

    return manifest


if __name__ == "__main__":
    m = freeze()
    print(f"Froze {m['file_count']} Generation 1 state files into "
          f"{SNAPSHOT_DIR.relative_to(REPO)}")
    for e in m["files"]:
        print(f"  {e['sha256']}  {e['file']}  ({e['size_bytes']} bytes)")
    print(f"set_sha256: {m['set_sha256']}")
