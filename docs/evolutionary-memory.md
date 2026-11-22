# Evolutionary memory pilot (one standalone bundle)

An **operator** selects this profile in the frontmatter of `meta/purpose.md`,
commits the configuration and any initial concepts, and starts the normal
MCP server on the bundle. Leave its auth/transport configuration unchanged.

```yaml
write_policy: proposals
direct: {profile: evolutionary-memory-v1}
```

Use this **instead of** `direct.types`, without a `preedit` policy. No init
default changes. If using an extraction plan with declared types, include
`Memory` to use plain-body creation. The operator can provision `dna.md`
as an ordinary valid OKF document. Starting a server does not create it.
Restart after changing profiles: tool registration is per server; writers
check policy again under the native review lock.

| Memory concept ID | Agent tools |
|---|---|
| `core/<id>`, `notes/<id>` | Direct create/edit or structural operation; optional proposal |
| Exactly `dna` (root `dna.md`) | Propose a create/update, or flag. No direct write, delete or structural replacement. Reviewing a proposal is a separate **operator** action. |
| Other paths, including metadata and generated navigation | Not memory write targets. No arbitrary file or Git operation. |

IDs are extensionless lower-kebab path segments. Choose the hierarchy;
there are no required thematic folders. Reserved names (including
`index.md` and `log.md`), links, multiply linked files and nested repo paths
are not mutation targets. Existing bundle/workspace read tools and their
paging, freshness and attribution contracts are unchanged. A workspace
doesn't expose the structural tool.

## Calls

All examples below are MCP tool **arguments** (results are JSON objects in
the normal MCP text block). `S` below means the actual raw-file SHA-256
from `okfy_show`, not a hash of rendered body text. A tool may not have shown
the entire page: don't submit truncated text as a replacement.

**Create or edit** with the existing
`okfy_write(concept_id, expect_sha256, actor?, content? | patch?, note?, evidence?, operation_id?)`.
For example:

```json
{"concept_id":"notes/experiments/reading","expect_sha256":"absent","actor":"agent:test","content":"Grain sizes differ.\n\nThe second sample was finer.","evidence":{"kind":"external-source","ref":"synthetic:sample-2"},"operation_id":"41a08102-bc83-4425-8ce6-f10362a40980"}
```

* `absent` means *create only*; an update requires the shown `S`. Exactly
  one of `content` or existing ordered body patch hunks
  (`[{old,new,count?}]`, count defaults to 1). No automatic rebase.
* For ordinary memory, plain `content` replaces the body, preserving metadata
  on update. On create, the writer supplies type `Memory`, title from the
  final slug, and description from the first nonempty line (up to 160
  characters), plus the native `generated` attribution and fresh `okf_id`.
  Content beginning with `---` is parsed as full OKF Markdown instead.
  Declared-type, schema, owner-field, identity, source, injection and secret
  checks still apply. Full documents may set a meaningful title/description.
* Supply provenance for new claims, in context in the text, or with
  `evidence:{kind,ref,quote?}` on the write. Kinds are `test-run`,
  `external-source`, `owner-decision`, `agent-inference`; only the last may
  omit the ref. Supplied evidence is recorded on the current concept, not
  authenticated. Ordinary edits can use any source: a message, observation,
  document, tool or inference. Corpus-relative `sources:` retain their
  original check; an external evidence ref is not fetched. OKFy doesn't
  infer a source or promote a claim of an owner decision to permission.
* `actor` is required unless server-verified identity supplies it. HTTP
  write scope and actor resolution are identical to `okfy_propose`.
  A declared actor string is not authentication.

**Reorganize** with
`okfy_reorganize(action, source, destination, bases, operation_id, actor?, extract?)`.
For example, after showing the page above, call (substitute its actual digest):

```json
{"actor":"agent:test","action":"split","source":"notes/experiments/reading","destination":"core/sample-two","bases":{"notes/experiments/reading":"S","core/sample-two":"absent"},"extract":"The second sample was finer.","operation_id":"ef892d69-ee2d-4568-9b6a-36c906d48352"}
```

| Action | Destination | What changes |
|---|---|---|
| `move` | Must be absent | Relocate one page (preserve metadata and stable ID); rebase native links. No content replacement parameter. |
| `split` | Must be absent | Move one exact, unique, nonempty body passage bounded by blank lines/page ends. Keep other content at source, replacing the passage with an inline link. Mint a new ID; inherit type and declared sources/evidence. Only split takes `extract`. |
| `merge` | Must exist | Append source body to destination with a paragraph break, keep destination identity, retire source. Move/merge retire a path, not its Git history. |

For every action, `bases` must be **exactly** source, destination, and
any other ordinary page whose body will change. Get backlinks from
`okfy_links(source)`, and hashes from `okfy_show`. No extra operand
authorizes extra edits. Source/destination must be distinct. Multi-page
staleness and destination collisions refuse the entire operation. Split/merge
require simple page metadata (type/title/description, identity, generation,
sources/evidence); merge requires equal type and provenance fields. This
avoids silently reattributing or dropping owner fields. Use ordinary edits
for semantic synthesis; this is not a general batch replace/delete API.

**Link boundary:** the live graph is OKF's existing inline body links
(`[label](relative.md)` / `[label](/root.md#fragment)`); it isn't a full
Markdown parser. Scan the active concepts, including metadata and DNA;
an incomplete scan refuses. Relocated recognized relative concept links
become root-relative; incoming links to a retired source are redirected.
Preserve URI links and move fragments. Refuse edits to a protected linker,
unresolvable relative targets in relocated text, and split/merge involving
recognized fragment links to the affected pages rather than guessing which
heading survives. Retirement also refuses native supersession, typed
evidence, lexicon, purpose-suite and open proposal target/successor references
to the old ID. No workspace-wide rewriting, wiki/HTML/reference-style link
repair, free-text citation editing, pending proposal body edits or index
regeneration. Dangling links already unrelated to this operation are not
validated. Packaged navigation remains a snapshot; use
`okfy_overview(listing=true)` for the live inventory.

**DNA:** `okfy_write` on exactly `dna` uses the existing base-checked
proposal fallback (full OKF Markdown or, for update, body patch). Omit
`operation_id`. Or call `okfy_propose(target_id="dna", action="update",
note=..., content=..., actor=..., evidence=...)` directly. Proposal target
paths are checked at filing and consumption; `supersedes` replaces a
*proposal*, never an active page. An agent can propose initial DNA while
absent but cannot bootstrap active DNA by calling a tool.

## Results and reconciliation

* **Committed:** structural success is
  `{effect:"committed", persisted:true, channel:"direct", operation_id, commit, bases, versions, replayed}`.
  Both maps use actual file hashes, or `"absent"`. A direct `okfy_write`
  adds `{id, path:"<id>.md", proposal:null, action:"create"|"update", sha256, origin:"agent-direct"}`.
  Each changed ID has its own native `accept` event with `origin:agent-direct`,
  actor, create/update/delete action and `receipt:{id,request,operation,sha256}`.
  `request` is a SHA-256 of the canonical request including effective actor,
  not the session trace. The original commit contains all those rows, the log
  and the concept transitions; removed bytes are in its parent. History is by
  path; `okfy_diff` still requires a live path with a known committed base.
* **Filed, not active or committed by this call:** pilot proposal success
  has `{effect:"filed", channel:"proposal", proposal, path, persisted:true}`
  plus the usual optional proposal diagnostics. Filing writes a proposal and
  appends its event to `meta/memory.jsonl`; it doesn't commit them, accept the
  proposal, or change active DNA. A later direct commit of the ledger may
  include those pending proposal rows. Proposal IDs aren't operation receipts.
* **No-op:** byte-identical direct update after serialization returns the
  direct write fields, `effect:"no-op", persisted:false, replayed:false`
  and equal bases/versions, **without** a commit, log entry or receipt. A
  no-op doesn't reserve its UUID.
* **Refused unchanged by this call:** `{effect:"refused", error, message, way_out}`.
  A stale single write can also carry `current_sha256, changed_by, changed_at,
  commit`. Re-read and use a new ID for a new intent. `E_MEMORY_ROLLBACK`
  includes `operation_id`: the before-images and clean index were restored at
  unchanged HEAD. The review-lock/cache file can exist even after refusal.
* **Uncertain:** `{effect:"uncertain", error:"E_MEMORY_UNCERTAIN", message,
  way_out, operation_id?}`, no success claim. Structural errors returned by
  the persistence step can also carry `replayed:false`. No transport reply
  or SDK failure is **not** evidence that nothing landed.

Choose a fresh lowercase hyphenated UUID **before** a direct mutation; it's
required for structural operations and generated if omitted for a single
write. Keep the request. After a lost reply, resending the identical request
under the same identity/ID can return `replayed:true` and the **original**
commit/versions even after intervening writes. This is a mutating retry, not
a read-only status query: with no receipt it can still apply if every base
and precondition holds. A changed request with an already committed ID is
refused. A replay is not a claim about the *current* text. Verify freshness
separately. Working-tree history rows, an actor name, matching bytes, or a
Git message alone cannot establish that the operation committed. Recovery
checks the ID trailer, the new request-bound ledger receipts in the same
commit, every operand's parent/result blob and the complete changed path set.

**Limits:** one trusted, standalone, normally configured Git repo with an
intact first-parent history. Touched concept bytes (including absence) must
match HEAD; the index must be clean. Mutators share OKFy's advisory review
lock. Reads do not: listing/show can observe intermediate filesystem states.
There is no crash-atomic filesystem transaction. On write failure the
instrument attempts path-scoped rollback only at unchanged HEAD. A crash,
failed rollback, manual/concurrent shell edits, Git filters/hooks, history
rewrite, branch switch or an incomplete receipt can require operator
inspection of the worktree, index, ledger and commits; don't blindly submit
a new ID. An error on retry does not disprove an earlier partial effect.
Keep operator maintenance offline. No exactly-once claim across clones,
other writers or stores, and no idempotency promise for proposals.

Nothing here activates DNA as a system prompt: the recipe/operator owns that
decision. No sync daemon, host-file editor, new access policy or automatic
outbound disclosure. Bundle text is data, not authority. Audience and privacy
at later use are runtime responsibilities, not enforced by these paths.
