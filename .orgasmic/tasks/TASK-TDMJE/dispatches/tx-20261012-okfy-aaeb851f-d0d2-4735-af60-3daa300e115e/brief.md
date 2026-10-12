# Review brief: OKFy direct-write lane

## What to review

One commit, checked out in your worktree: branch `feat/direct-write-lane`,
range `9618b9b..9421034` (one commit on top of upstream `main`, v0.29.0).
`git show --stat 9421034` lists the files.

The upstream project is public and has a strong house style: small named
refusals with a code and a way out, an additive MCP response contract, a ledger
row for every content write, one shared predicate instead of two readers of the
same thing. `git log -8` and `CHANGELOG.md` show the voice. A change that is
correct but would not be accepted upstream is a finding.

## The design the implementer was given

- Problem: an agent can only file a proposal and wait for the owner. Some
  concept types (session notes, observations, logs) should be writable at once,
  so several agents see each other's writes, while everything else keeps going
  through `proposals/`.
- Policy: `meta/purpose.md` keeps `write_policy: proposals` and gains an
  optional `direct: {types: [...]}`. No new `write_policy` value. A closed
  schema: unknown keys are an error, and the block is an error under
  `write_policy: direct`.
- Routing: by the target concept's current `type` on an update, by the proposed
  `type` on a create. Retyping across the boundary is refused.
- Concurrency: optimistic. The writer presents the sha256 of the raw file bytes
  it read (the digest `okfy_show` already returns), and the write is refused if
  the file changed since. No new locking primitive: the existing `review_lock`.
- Gates: the injection and secret scans and the validation `review accept`
  applies are reused, not duplicated. The proposal-only gates (duplicate title,
  rejected-content tombstone, gap cap) do not apply.
- Write: atomic (temp file in the same directory, then rename).
- Record: a ledger row with event `accept`, `proposal: null` and
  `origin: agent-direct`, a `log.md` line, one commit through `_commit`.
- MCP: a new tool `okfy_write`. When the core refuses because the type is not in
  the lane or the bundle has no lane, the adapter files a proposal instead and
  returns it with `channel: "proposal"`; a direct write returns
  `channel: "direct"`. Existing response shapes only gain fields.
- CLI: `okfy write`, a thin wrapper, no fallback.
- Out of scope: batching commits, multi-file writes, routing by directory, and
  hardening `okfy refine`.

## What the change claims

Read these as claims to test, not as facts.

1. A concept of a listed type is written by an agent at once; everything else
   still goes to `proposals/`.
2. The check and the write happen in one step under `review_lock`. Of two
   writers holding the same sha256 exactly one lands; the other gets
   `E_WRITE_BASE_MOVED`.
3. The lane relaxes review and nothing else: the scans, the `sources:` check and
   the archetype schema are the same functions a proposal passes.
4. A direct write cannot change `type`, cannot change the fields an owner
   decision writes (`verified`, `generated`, the stale fields,
   `supersedes`/`superseded_by`), and cannot reach `meta/` or any other reserved
   directory, so the policy cannot be used to rewrite itself.
5. `okfy_write` routes by the bundle's policy, and its proposal fallback is
   filed against the same sha256. No existing tool's response shape changed.
6. An agent's direct write does not dispose of an owner's standing rejection
   (`okfy.update.unresolved_rejections`).

## Deviations from the design, made on purpose

Judge each one: right call, wrong call, or the upstream maintainer's decision.

- 13 new codes instead of the 9 the design listed. Added: `W_DIRECT_TYPE`,
  `E_DIRECT_OWNER_FIELD`, `E_WRITE_CONTENT`, `E_WRITE_WORKSPACE`.
- The owner-field protection in claim 4 is not in the design.
- The proposal fallback checks the base too, through a new core function
  `proposals.propose_checked`, rather than a new keyword on `propose()`. The
  stated reason: an upstream test that is not in the public tree may pin
  `propose()`'s signature against `BATCH_ENTRY_KEYS`.
- The file is always re-serialized (`frontmatter.serialize`), never written
  verbatim, including on the full-text path.
- The scan half of `_gate` was extracted into `_refuse_scan`, and the declared
  type lookup in `validate._check_types` into `_declared_types`. Both refactor
  existing code: check they change no behaviour and no message text.
- The `CHANGELOG.md` entry is titled "Unreleased" and no version is bumped.
- Two test files were added to a tree that ships none by policy
  (`.github/workflows/ci.yml` explains the policy).
- `okfy refine` was left untouched although it neither scans, validates nor
  locks.

## Where to look hardest

- `core/src/okfy/proposals.py`: `write_direct`, `_write_direct`,
  `_base_capture`, `_refuse_base_moved`, `_atomic_write`, `propose_checked`.
  Trace every path from entry to the rename. For each refusal, confirm nothing
  was written before it. Look for a path where the lane check, the base check
  and the write can see different bytes, or different types.
- Ways around claim 4: an id that resolves somewhere other than where it says
  (`..`, symlinks, letter case, a trailing `.md`, an id that names `index`,
  `log`, `README`, `AGENTS`), a type that is not a string, YAML that parses to
  something equal-but-different for a protected field, a key the protected list
  misses. Say what you tried and what happened. This is ordinary input
  validation review of a function that writes files.
- The failure order after the rename: ledger row, `log.md`, git commit. What
  state is left if each step fails, and is that the exposure `_accept` already
  has or a new one?
- Every other reader of `accept` rows in `meta/memory.jsonl`
  (`okfy.update._protected`, `okfy.memory.accepted_since`, `okfy.release`,
  `okfy.memory.rejected_match_hashes`). Is an `accept` row written by an agent
  wrong for any of them? Was reusing the `accept` event the right call?
- `adapters/mcp/src/okfy_mcp/handlers.py`: `h_write`, the change to
  `h_propose`, `_refusal_envelope`. Is there a refusal that escapes as a raw
  exception instead of `{error, message, way_out}`? Does the fallback ever file
  a proposal the caller did not ask for, or hide an error it should return?
- `core/src/okfy/validate.py`: `direct_policy`, `_check_direct`. Is there a
  malformed `direct` block that opens the lane, or a well-formed one that
  validate rejects?
- `core/src/okfy/codes.py` against the raise sites: every new code raised is
  registered, and every summary describes what its site really does.
- The tests (`core/tests/test_direct_write.py`,
  `adapters/mcp/tests/test_write_tool.py`): would each one fail if the logic it
  names were broken? Name any that pass for the wrong reason, and any behaviour
  above that no test covers. The concurrency test is the one most likely to be
  weaker than it looks.
- The docs (`README.md`, `adapters/mcp/README.md`, `docs/guide/GUIDE.md`,
  `docs/OKF-COMPATIBILITY.md`, `CHANGELOG.md`): any sentence the code does not
  back.

## What you may and may not do

- Do not edit tracked files, commit, or push. Do not touch any remote.
- Running the tests IS allowed and is not a source edit. So is creating
  virtualenvs and writing throwaway scripts and temporary bundles outside the
  tracked tree. `uv run` creates a gitignored `.venv` in `core/` and
  `adapters/mcp/`; that is fine.
- To run the suites:
  - `cd core && uv run pytest -q`
  - `cd adapters/mcp && uv run pytest -q`
  - `uv run ruff check .` in each of the two directories
- `scripts/smoke.sh` needs `okfy`, `okfy-mcp` and `okfy-normalize` installed
  from the built wheels on `PATH`. Run it only from a throwaway virtualenv; skip
  it and say so if that is not practical.
- To probe behaviour, write a small script against a temporary bundle
  (`okfy.init.init_bundle` in a temp directory, as the tests' fixture does).
- Keep load low: no parallel test loops, no stress runs.

## What to hand back

The report is published with this project's ledger, so write it for a public
reader: repository-relative paths only, and no absolute paths, user names, host
names, e-mail addresses or environment values.

1. Verdict: `approve`, `approve-with-follow-ups` or `reject`.
2. Findings, most severe first. For each: severity (HIGH / MEDIUM / LOW),
   `file:line`, what is wrong, a concrete input or sequence that shows it, and
   whether you reproduced it or only read it. Say which.
3. For each deliberate deviation above: right, wrong, or the maintainer's call.
4. What you ran and its result, and what you did not check.

If you find nothing at a severity, say so rather than padding the list.
