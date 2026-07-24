# Generation-2 recovery runbook

This is the operator's guide for when something goes wrong with the Generation-2
paper-trading experiment. It assumes **nothing about a local machine**: every
step here is performed from the GitHub web UI, the `gh` CLI, or a clean checkout
on any machine. The authoritative environment is the GitHub-hosted Ubuntu runner,
never a laptop.

Everything Gen2 does is **simulated paper trading against public price candles —
no real money, no exchange orders, no credentials.** "Recovery" here means
restoring a correct, verifiable published state; it never involves money.

## The mental model (read this first)

Two workflows, deliberately separate:

| Workflow | File | Trigger | Can write repo? | Job |
| --- | --- | --- | --- | --- |
| **Coordinator** | `.github/workflows/gen2-coordinator.yml` | `workflow_dispatch` only (no cron yet) | `contents: write`, only inside `state/gen2/<active id>/` | Runs ONE tick: fetch candles → run 8 bots → publish checkpoint → commit + push |
| **Pages** | `.github/workflows/gen2-pages.yml` | `workflow_dispatch` + push to renderer/state paths | **No** `contents: write` | Renders the committed `CURRENT` checkpoint to a static site and deploys it |
| CI | `.github/workflows/ci.yml` | push / PR | No | Offline test suite; never trades |

Key invariants the recovery steps rely on:

- **Nothing runs on its own.** There is no `schedule` and no `repository_dispatch`
  anywhere. Until cron is approved (Gate L2), the coordinator ticks *only* when a
  human dispatches it. **So the first move in almost every incident is simply: do
  not dispatch the coordinator.** That is a full stop.
- The coordinator refuses to tick unless exactly ONE eligible experiment exists
  and its manifest `status == ACTIVE`. Retired and terminal experiments are
  discarded during resolution.
- Every published state is verified by hash on the way in *and* on the way out.
  A corrupt `CURRENT` fails closed — it is never rendered or pushed over.

Status values (`algotrading/gen2/experiment.py`):
`PREPARED` → `ACTIVE` → (`PAUSED` ⇄ `ACTIVE`) ; terminal: `FAILED_CANARY`, `CLOSED`.
`FAILED` marks an aborted tick that needs investigation before resuming.

---

## 1. Pause the experiment (stop trading immediately)

**Fastest stop (no commit):** do not dispatch `gen2-coordinator`. With no cron
enabled, not dispatching *is* a pause — nothing will tick. If you want to make
the workflow un-runnable even by mistake, disable it: GitHub → **Actions** →
*gen2-coordinator* → **⋯** → **Disable workflow**.

**Durable pause (survives a future cron):** flip the manifest status so the
tick job's `if: … == 'ACTIVE'` guard skips, from any clean checkout:

```bash
# edit state/gen2/<id>/manifest.json:  "status": "ACTIVE"  ->  "status": "PAUSED"
git checkout -b pause-gen2
git add state/gen2/<id>/manifest.json
git commit -m "gen2: pause experiment <id> (operator hold)"
git push origin pause-gen2      # open a PR, or push to main if you have rights
```

A `PAUSED` experiment is still the single eligible experiment (not terminal), so
the resolver succeeds but the tick job does not run. Resume by setting it back to
`ACTIVE` the same way. **Never hand-edit any file other than `manifest.json`'s
`status`** — the checkpoints and pointers are hash-bound.

---

## 2. Disable cron

Cron is **not enabled** during Gate L1 — there is nothing to disable yet. When it
is added at Gate L2, disable it by either:

- **Actions UI:** *gen2-coordinator* → **⋯** → **Disable workflow** (stops all
  triggers, including schedule), or
- **Edit the workflow:** remove/comment the `schedule:` block and push. The
  `workflow_dispatch` entry can stay so you can still run a manual, supervised
  tick.

Confirm afterward: Actions → *gen2-coordinator* shows no scheduled runs queued.

---

## 3. Inspect a failed Action

1. GitHub → **Actions** → open the failed *gen2-coordinator* run.
2. Read `resolve-experiment` first. If it failed, the message says whether it saw
   **zero** eligible experiments or **more than one** (both fail closed). Fix the
   state set (see §10) rather than the workflow.
3. If `tick` failed, the likely stages and meanings:
   - **Verify CURRENT before computing** failed → the published pointer or an
     artifact hash does not match. Do **not** re-dispatch. Go to §4.
   - **Run ONE … tick** failed → a required bot or the market fetch aborted. The
     tick publishes atomically, so a mid-tick failure leaves **no** partial
     checkpoint. Verify that (`verify-current`, §4), then investigate the log.
   - **Persist … (rebase-revalidate)** failed with a *rebase conflict* → a newer
     run already advanced this experiment; this is the anti-clobber guard working.
     See §9.
   - The **out-of-scope guard** (`Tick dirtied paths outside …`) fired → the tick
     touched a file outside its own experiment dir. Treat as a bug; do not push.
     Leave state as-is and investigate.
4. Whatever the cause: the run makes **no** change unless the final push
   succeeded. A failed run is safe to leave; recovery is about verifying, not
   undoing.

---

## 4. Verify CURRENT (the health check you run constantly)

From any clean checkout on any machine (Ubuntu authoritative):

```bash
pip install -r requirements.txt
python -m algotrading.gen2 --experiment-id <id> verify-current
```

- Exit 0 + "CURRENT verified" → the published state is fully hash-consistent;
  the experiment is healthy and safe to resume/redeploy.
- "no published checkpoint (CURRENT is absent)" → genesis not yet run; expected
  before the first tick.
- "INTEGRITY FAILURE …" (exit 2) → the pointer/artifacts do not verify. **Stop.**
  Do not dispatch, do not deploy Pages. Go to §5.

This command is read-only; it never writes state.

---

## 5. Recover an orphan / integrity failure

An "orphan" is a checkpoint directory that `CURRENT` does not point to (e.g. a
tick wrote a checkpoint but the push half-failed), or a `CURRENT` that fails
verification.

Because the coordinator publishes atomically and pushes **only after**
re-verifying, and never force-pushes, the committed history on `main` is the
source of truth:

1. Run `verify-current` (§4). If it passes, there is no orphan — the extra
   directory is inert and referenced by nothing; leave it (evidence) or remove it
   in a dedicated commit only after confirming `CURRENT` still verifies.
2. If `verify-current` fails, the last **good** state is the previous commit that
   did verify. Identify it:
   ```bash
   git log --oneline -- state/gen2/<id>/CURRENT
   ```
   Check out a candidate commit into a scratch worktree and run `verify-current`
   against it until you find the newest verifying one.
3. Restore by **reverting forward**, never by force-push or history rewrite:
   ```bash
   git revert --no-edit <bad-commit>     # or git checkout <good-commit> -- state/gen2/<id>/
   git add state/gen2/<id>/
   git commit -m "gen2: restore last verified CURRENT for <id>"
   ```
4. Re-run `verify-current` on the restored tree. Only when it passes may you
   resume (§1) or redeploy Pages (§9).

Never edit checkpoint bytes or pointer hashes by hand — regenerate by reverting to
a state that already verifies.

---

## 6. Coinbase outage or stale data

The feed is Coinbase's **keyless public** candle API. If it is down or lagging:

- **During a tick:** the tick fetches once and freezes one hashed snapshot. If the
  fetch fails or candles are too old, the tick aborts and publishes nothing —
  correct behaviour. Do not retry in a tight loop; wait for the feed to recover.
- **At activation:** `activate` runs a launch-time provenance gate that makes its
  own real request. On outage it exits `BLOCKED`; on uncertified/stale data
  `MARKET_DATA_PROVENANCE_FAILED`. Both refuse to activate. Re-run `activate` once
  the public endpoint is healthy.
- **Check the feed yourself** without touching state:
  ```bash
  python -m algotrading.gen2 preflight            # summary
  python -m algotrading.gen2 preflight --json      # full report
  ```
- **On the dashboard:** the Pages site shows the *data age* and raises a
  **"Stale data"** warning once the newest candle is older than the stale
  threshold (`STALE_AFTER_S`, ~2h15m = one hourly cadence + slack). Stale is a
  display warning, not corruption — no recovery action beyond waiting, unless age
  keeps growing, which means ticks are not running (check §1–§3).

A missed hour is not an error: when ticking resumes it selects the latest closed
candle newer than `CURRENT`; it never fabricates or back-fills skipped candles.

---

## 7. Rejected push / race between runs

Concurrency (`gen2-coordinator-<id>`, `cancel-in-progress: false`) serialises
same-experiment runs, so a true race is rare. If a push is rejected:

- The persist step already retries up to 5×: `fetch` → `rebase origin/main` →
  **re-verify CURRENT** → `push`. It never uses `--force`.
- A **rebase conflict** is treated as "a newer run already published" and fails
  closed (no clobber). This is expected and safe: the other run's state stands.
- If all 5 attempts are exhausted by transient rejections, the run fails with
  "Failed to persist … after 5 attempts." Recovery: run `verify-current` (§4). If
  it passes, the experiment is fine — simply dispatch one more supervised tick
  when ready. Do **not** force-push to "win" the race.

---

## 8. Redeploy the last-good dashboard

The Pages workflow is read-only and idempotent — re-running it just re-renders
committed state:

1. First confirm the state it will render is good: `verify-current` (§4).
2. GitHub → **Actions** → *gen2-pages* → **Run workflow** (on `main`).
3. The `build` job renders `CURRENT`; if it cannot verify, it exits non-zero, the
   `deploy` job is skipped, and **the previously deployed site stays live**. A
   corrupt store can never replace a good published page.
4. If only a retired/terminal experiment exists, the build writes an honest
   **placeholder** page ("no active Generation-2 experiment") rather than fake
   numbers — deploying that is correct, not a failure.

There is no separate "roll back the dashboard" step: redeploy from the last commit
whose `CURRENT` verifies (find it as in §5), and Pages will render exactly that.

---

## 9. Close an experiment WITHOUT deleting evidence

Closing is terminal and **append-only**. Never delete a manifest, checkpoint,
preflight report, or failure record — they are the audit trail.

To close normally:

```bash
# edit state/gen2/<id>/manifest.json:  "status" -> "CLOSED"
git add state/gen2/<id>/manifest.json
git commit -m "gen2: close experiment <id> (terminal, evidence preserved)"
git push …
```

`CLOSED` (like `FAILED_CANARY`) is terminal: the resolver discards it, so it can
never be ticked again, yet all its files remain in history. A closed experiment
may coexist on disk with a new `PREPARED`/`ACTIVE` one — the resolver still
requires exactly one *eligible* experiment.

For a **canary that failed under a portability/source-binding defect**, use
`FAILED_CANARY` and write the append-only record (see the retired experiment
`state/gen2/gen2-20260724T023914Z-b91b8e74/FAILED_CANARY.json` as the template):
the id is added to `RETIRED_EXPERIMENT_IDS`, is never repaired or reused, and a
**fresh** experiment id is minted instead. Do not resurrect a terminal id.

---

## Quick reference

| Symptom | Section |
| --- | --- |
| "Stop trading now" | §1 (just don't dispatch) |
| Turn off the schedule | §2 |
| An Action went red | §3 |
| "Is the published state OK?" | §4 |
| CURRENT won't verify / stray checkpoint dir | §5 |
| Feed down / dashboard says stale | §6 |
| Push rejected / rebase conflict | §7 |
| Public site looks wrong/old | §8 |
| Retire or close an experiment | §9 |

When in doubt: **do not dispatch, run `verify-current`, and change nothing that is
hash-bound.** Fail closed beats guessing.
