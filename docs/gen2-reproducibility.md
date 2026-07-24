# Generation-2 reproducibility & clean-runner operation

**Non-negotiable requirement (Message B):** after one-time setup, Generation-2
must be fully deployable and operable from a clean **GitHub-hosted Ubuntu
runner**. It must not depend on any developer's Windows machine, local
uncommitted files, a local cron, manual state copying, Windows paths or line
endings, locally-installed packages, external servers, or exchange credentials.

Everything Gen2 does is **simulated paper trading against public price candles —
no real money, no orders, no secrets.**

This document is the proof and the procedure: what is committed, how a bare
`git clone` reproduces the full pipeline, and how the source-binding hash is
verified to match byte-for-byte on Linux.

---

## 1. The authoritative environment

| | |
| --- | --- |
| OS | `ubuntu-latest` (GitHub-hosted runner) |
| Python | 3.11 (`actions/setup-python@v5`) |
| Dependencies | exact pins in `requirements.txt` (`pip install -r requirements.txt`) |
| Market data | Coinbase **keyless public** candle API over HTTPS (`requests`) |
| Auth | built-in `GITHUB_TOKEN` only; **no** exchange keys, **no** repo secrets |
| State | committed, hash-bound checkpoints under `state/gen2/<id>/` |
| Dashboard | static GitHub Pages, rendered from committed state |

A local Windows checkout is a convenience for development only. **Local success
is not sufficient evidence** — the source-binding hash and the canary must be
proven on the GitHub Ubuntu runner (see §5, §6).

---

## 2. One-time repository setup (manual, cannot be scripted here)

Exactly one manual step is required, done once in the GitHub web UI:

> **Settings → Pages → Build and deployment → Source = "GitHub Actions".**

No branch is served directly; the `gen2-pages` workflow is the Pages source. This
cannot be set from the CLI in this environment. Everything else below is already
committed and needs no setup.

(If/when the recurring hourly cron is approved at Gate L2, enabling it is a second
manual/committed step — out of scope for this document.)

---

## 3. What is committed to operate (and what is deliberately not)

**Committed — everything needed to run from a bare clone:**

- **Pinned dependency metadata** — `requirements.txt`, exact `==` pins, all with
  manylinux wheels so a clean runner needs no build toolchain.
- **`.gitattributes`** — forces `*.py`/`*.yml`/`*.yaml`/`*.json` to LF in the
  working tree, so a Windows checkout and the Ubuntu runner hold identical bytes.
  This is a *belt*; the newline-normalizing canonical hash (§5) is the
  *suspenders* and is required on its own.
- **Source inventory** — `algotrading/gen2/source_inventory.json`, the declared
  list of files that participate in the binding hash (list only, not content
  hashes).
- **Genesis checkpoint + `CURRENT`** — once a fresh experiment is prepared, its
  immutable genesis checkpoint and pointer live under `state/gen2/<id>/`.
- **Dashboard builder** — `algotrading/gen2/dashboard.py` +
  `algotrading/gen2/__main__.py build-pages` render the static site from
  committed state.
- **Workflows** — `.github/workflows/`:
  - `ci.yml` — offline test suite + LF/CRLF portability evidence + prints the
    Linux canonical source hash. `contents: read`.
  - `gen2-coordinator.yml` — the manual (`workflow_dispatch`) tick.
    `contents: write`, scoped to `state/gen2/<active id>/`.
  - `gen2-pages.yml` — read-only render + deploy. `pages: write` + `id-token:
    write` (required by `actions/deploy-pages`), **no** `contents: write`.
- **Config** — `config/config.ci.yaml` (the roster/params/costs used on the
  runner) and examples.
- **These docs** — this file + `docs/gen2-recovery.md`.

**Deliberately NOT committed:** exchange credentials or any secret, `pip`/HTTP
caches, virtualenvs, `__pycache__`, temporary staging dirs, or a developer's
local dashboard preview (`dashboard_preview.html`, `evidence/**/dashboard_preview.html`).

---

## 4. Reproduce the full pipeline from a clean clone

On any clean machine (Ubuntu authoritative), with no prior local state:

```bash
git clone <repo-url> && cd <repo>
python -m pip install --upgrade pip
pip install -r requirements.txt

# 1. Offline suite must be green (this is exactly what CI runs).
python -m pytest -q

# 2. Read-only feed check (keyless, no state written).
python -m algotrading.gen2 preflight

# 3. Verify the published state resolves after a FRESH checkout
#    (proves state is self-contained in the repo, not on any laptop).
python -m algotrading.gen2 --experiment-id <id> verify-current

# 4. Build the public dashboard from committed state (read-only).
python -m algotrading.gen2 build-pages --out site
test -f site/index.html
```

The end-to-end trading pipeline (fetch → run 8 bots → publish checkpoint →
commit → push → Pages render → deploy) runs as two workflows on the runner:

- `gen2-coordinator` performs `tick → publish → commit → push` into
  `state/gen2/<id>/` (fetch/rebase/re-verify/push, no force, idempotent).
- `gen2-pages` performs `render → deploy` from the committed `CURRENT`.

No step reads a Windows path, a local file outside the clone, or a credential.

---

## 5. Source-binding hash: prove Linux == Windows

The experiment binds to its source via the canonical hash
`python-source-canonical-sha256` **v2** (`algotrading/gen2/source_hash.py`). v2
decodes UTF-8 strictly and normalizes CRLF/CR → LF **before** hashing, with
length-prefixed framing and bytewise-sorted repo-relative POSIX paths — so the
same tree hashes **identically** whether checked out with LF (Linux) or CRLF
(Windows). This is the fix for the original defect, where raw-byte v1 hashing made
the Windows value differ from the Linux value and the canary failed closed.

Compute the hash on any machine:

```bash
python - <<'PY'
from algotrading.gen2 import source_hash as sh
h = sh.canonical_source_hash()
for k in ("algorithm", "version", "sha256", "file_count", "inventory_sha256"):
    print(f"{k}={h[k]}")
PY
```

**The check that must pass before launch:** the `sha256` printed locally on
Windows must equal the value the CI step *"Record the Gen2 canonical source hash
(Linux authoritative value)"* prints on the Ubuntu runner, **byte-for-byte**. The
CI job `ci.yml` also runs the LF/CRLF/mixed/lone-CR portability tests verbosely so
their PASS status is named evidence in the Linux log.

If the two values differ, the portability remediation has regressed — **stop** and
treat it as `SOURCE_BINDING_REMEDIATION_FAILED`; do not prepare or activate an
experiment against a hash that is not reproducible on the authoritative runner.

Note: editing the *content* of an already-listed `.py` file changes the `sha256`
(as it should) but not `inventory_sha256` or `file_count`; the inventory only
records the *set* of files, and the drift check compares that set. Adding or
removing a `.py` file under `algotrading/` requires regenerating the inventory.

---

## 6. Deployment proof required before cron (Message B §7)

All of the following must hold on the GitHub runner (not just locally) before the
recurring cron may even be proposed:

1. Linux CI (`ci.yml`) passes on the remediation commit.
2. LF and CRLF trees hash to the same value; the CI-recorded Linux `sha256`
   equals the local Windows `sha256`.
3. A fresh manual canary (`gen2-coordinator` via `workflow_dispatch`) succeeds on
   the runner for a freshly-prepared experiment.
4. The coordinator publishes a checkpoint that `verify-current` accepts.
5. `CURRENT` resolves after a fresh checkout (§4 step 3).
6. A second dispatch is idempotent — it does not double-publish or corrupt state.
7. Pages deploys and the public URL shows the correct Gen2 canary state.
8. Generation-1 remains untouched (`INVALIDATED_IMPLEMENTATION`).

Only after all eight are verified does the process stop at
`READY_FOR_CRON_APPROVAL`. If the project cannot be operated from a clean GitHub
runner, the status is `GITHUB_DEPLOYMENT_BLOCKED`.

---

## 7. Permissions (least privilege, Message B §3)

| Workflow | Permissions | Why |
| --- | --- | --- |
| `ci.yml` | `contents: read` | only reads checked-out code to run tests |
| `gen2-coordinator.yml` | `contents: read` default; `contents: write` on the `tick` job only | must commit state; scoped to `state/gen2/<active id>/`, guarded against writing anything else |
| `gen2-pages.yml` | `contents: read`, `pages: write`, `id-token: write` | official `actions/deploy-pages` requires `pages: write` + OIDC `id-token: write`; it needs no repo write |

No `issues`, `pull-requests`, or `packages` scopes are granted anywhere. Only the
built-in `GITHUB_TOKEN` is used; there are no repository secrets.

---

## 8. See also

- `docs/gen2-recovery.md` — incident runbook (pause, disable cron, inspect a
  failed Action, verify/restore CURRENT, feed outage, push races, redeploy the
  dashboard, close an experiment without deleting evidence).
- `state/gen2/gen2-20260724T023914Z-b91b8e74/FAILED_CANARY.json` — the append-only
  record of the retired canary that this remediation fixes.
