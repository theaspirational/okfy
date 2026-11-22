"""Opt-in memory mutations. All entry points hold proposals.review_lock.

Not a general transaction API: callers plan only concept bytes; ledger, log,
commit and receipt verification are fixed. No repository or path arguments
from the wire reach git except checked ordinary concept ids.
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote

from okfy import frontmatter, memory, proposals as p
from okfy.actor import check_actor, utc_now
from okfy.bundle import RESERVED_FILES, listing_paths
from okfy.gitenv import run_git
from okfy.ids import new_id
from okfy.validate import LINK_RE, direct_policy, is_external, memory_profile, resolve_link


def refuse(message):
    raise ValueError(f"E_MEMORY_OPERATION: {message} — resolve the precondition before retrying")


def regular_path(bundle, path):
    """Reject aliases, special files and nested repositories (including on create)."""
    rel = path.relative_to(bundle.root)
    here = bundle.root
    for component in rel.parts:
        here = here / component
        try:
            st = here.lstat()
        except FileNotFoundError:
            continue
        if here.name not in os.listdir(here.parent):
            refuse(f"noncanonical path spelling: {rel}")
        if here == path:
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                refuse(f"not an unaliased regular file: {rel}")
        elif not stat.S_ISDIR(st.st_mode) or os.path.lexists(here / ".git"):
            refuse(f"not an ordinary bundle directory: {here.relative_to(bundle.root)}")
    return path


def ordinary_path(bundle, cid):
    p._validate_write_id(bundle, cid, flag="concept-id")
    path = bundle.root / f"{cid}.md"
    if (not cid.startswith(("core/", "notes/")) or path.name in RESERVED_FILES
            or not all(p._ID_SEGMENT_RE.fullmatch(part) for part in cid.split("/"))):
        refuse(f"{cid!r} is not an ordinary core/ or notes/ concept")
    return regular_path(bundle, path)


def proposal_targets(bundle, action, target, successor=None):
    """A review must name the real operand, not e.g. a symlink to DNA. Apply
    at filing AND operator acceptance (queue files and paths can change)."""
    if not target and action not in ("flag", "gap"):
        refuse("memory proposals require an explicit target")
    for cid in (target, successor):
        if cid == "dna":
            if action in ("delete", "supersede"):
                refuse("DNA may not be deleted or structurally replaced")
            regular_path(bundle, bundle.root / "dna.md")
        elif cid:
            ordinary_path(bundle, cid)


def minimal_metadata(cid, body):
    title = cid.rsplit("/", 1)[-1].replace("-", " ")
    summary = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if not summary:
        refuse("empty memory body")
    return {"type": "Memory", "title": title, "description": summary[:160]}


def validate_fields(meta):
    for key in ("type", "title", "description"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            refuse(f"expected nonempty text for {key}")
        meta[key].encode("utf-8")
    if "evidence" in meta:
        p.check_evidence(meta["evidence"])


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else p.ABSENT


def git(bundle, *args):
    try:
        return run_git(bundle.root, *args, check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"E_MEMORY_REPOSITORY: git {args[0]} failed — inspect the "
                         "standalone bundle repository") from exc


def head(bundle):
    return git(bundle, "rev-parse", "--verify", "HEAD").decode("ascii").strip()


def blob(bundle, rev, rel):
    # An absent tree entry and an unreadable git object are NOT the same answer.
    if not git(bundle, "ls-tree", "-z", rev, "--", rel):
        return None
    return git(bundle, "cat-file", "blob", f"{rev}:{rel}")


def _rows(data):
    rows, problems = memory._parse_lines((data or b"").decode("utf-8"))
    if problems:
        refuse(problems[0])
    return rows


class ReceiptConflict(ValueError):
    pass


class Operation:
    def __init__(self, bundle, operation_id, request):
        self.bundle = bundle
        self.id = str(uuid.uuid4()) if operation_id is None else operation_id
        if not isinstance(self.id, str) or not re.fullmatch(
                r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", self.id):
            refuse("operation_id must be a lowercase hyphenated UUID")
        self.request = digest(json.dumps(request, ensure_ascii=True, sort_keys=True,
                                         separators=(",", ":")).encode())
        self.expected = request["bases"] if "bases" in request else {request["write"]: request["base"]}
        marker = bundle.root / ".git"
        if marker.is_symlink() or not marker.is_dir() or (
                Path(os.fsdecode(git(bundle, "rev-parse", "--show-toplevel").removesuffix(b"\n")))
                != bundle.root):
            refuse("a standalone committed bundle repository is required")
        self.parent = head(bundle)
        candidates = git(bundle, "log", "--first-parent", "--format=%H",
                         f"--grep=^Okfy-Operation: {self.id}$", self.parent).splitlines()
        self.prior = False
        self.result = {}
        if candidates:
            self.prior = True
            try:
                if len(candidates) != 1:
                    refuse("ambiguous receipt history")
                self.result = self.verify(candidates[0].decode())
            except ReceiptConflict:
                raise
            except (KeyError, TypeError, ValueError, OSError):
                self.result = self.uncertain()

    def uncertain(self):
        return {"error": "E_MEMORY_UNCERTAIN", "effect": "uncertain",
                "operation_id": self.id, "message": "could not confirm commit or rollback",
                "way_out": "reconcile with the identical request; inspect if no receipt"}

    def verify(self, commit):
        """Proof is the new ledger receipt AND its concept transitions in one commit."""
        b = self.bundle
        parent = git(b, "rev-parse", f"{commit}^").decode().strip()
        marker = git(b, "show", "-s", "--format=%(trailers:key=Okfy-Operation,valueonly)",
                     commit).decode().strip()
        if marker != self.id:
            refuse("operation marker does not identify a unique receipt")
        old = blob(b, parent, memory.MEMORY_FILE) or b""
        new = blob(b, commit, memory.MEMORY_FILE) or b""
        if not new.startswith(old):
            refuse("receipt ledger is not append-only")
        versions = {}
        bases = {}
        for row in _rows(new[len(old):]):
            receipt = row.get("receipt", {})
            if not isinstance(receipt, dict):
                refuse("malformed receipt")
            if receipt.get("id") != self.id:
                continue
            if receipt["request"] != self.request:
                raise ReceiptConflict("E_MEMORY_OPERATION: operation_id already used for a "
                                      "different request — do not reuse IDs")
            cid = row["target"]
            if cid not in self.expected or row.get("origin") != "agent-direct" or row["event"] != "accept":
                refuse("unexpected receipt target or origin")
            rel = f"{cid}.md"
            base = row.get("base_sha256") or p.ABSENT
            if (cid in versions or digest(blob(b, parent, rel)) != base
                    or digest(blob(b, commit, rel)) != receipt["sha256"]):
                refuse("receipt does not match committed concept transitions")
            versions[cid], bases[cid] = receipt["sha256"], base
        changed = set(os.fsdecode(git(b, "diff", "--no-renames", "--name-only", "-z",
                                      parent, commit)).rstrip("\0").split("\0"))
        expected_paths = {f"{cid}.md" for cid in self.expected} | {"log.md", memory.MEMORY_FILE}
        if bases != self.expected or changed != expected_paths:
            refuse("missing or incomplete committed receipt")
        return {"effect": "committed", "commit": commit, "operation_id": self.id,
                "versions": versions, "bases": bases, "persisted": True, "channel": "direct"}

    def write_result(self, cid):
        if "error" in self.result:
            return self.result
        return {**self.result, "replayed": self.prior, "id": cid,
                "action": "create" if self.result["bases"][cid] == p.ABSENT else "update",
                "sha256": self.result["versions"][cid], "origin": "agent-direct"}

    def apply(self, action, actor, changes, note="", observed=None):
        """Preflight all before-images, then apply once. Rollback only at unchanged HEAD."""
        b = self.bundle
        log = regular_path(b, b.root / "log.md")
        ledger = regular_path(b, b.root / memory.MEMORY_FILE)
        p._refuse_if_ledger_rewritten(b, memory.MEMORY_FILE)
        p._refuse_unreadable_ledger(b, "a memory mutation")
        if git(b, "diff", "--cached", "--name-only", "-z"):
            refuse("index not clean; owner must settle staged changes")
        before = {}
        dirs = set()
        for cid, (old, new) in changes.items():
            path = ordinary_path(b, cid)
            # Absence in discovery isn't proof of absence on disk (including
            # case-folding filesystems); compare both file and committed blob.
            actual = path.read_bytes() if path.exists() else None
            if actual != old:
                p._refuse_base_moved(cid, digest(old), digest(actual))
            if blob(b, self.parent, f"{cid}.md") != old:
                refuse(f"{cid} base must match committed bytes (including absence)")
            before[path] = old
            dirs.update(d for d in path.parents if d.is_relative_to(b.root) and not d.exists())
        for path in (log, ledger):
            before[path] = path.read_bytes() if path.exists() else None
        committed_log = blob(b, self.parent, "log.md")
        if before[log] != committed_log:
            refuse("uncommitted log; owner inspection required")
        # Open proposals normally leave uncommitted ledger appends. Carry those
        # alongside our receipt, but never legitimize an interrupted direct write.
        baseline = blob(b, self.parent, memory.MEMORY_FILE) or b""
        live = before[ledger] or b""
        if not live.startswith(baseline) or any(
                row["event"] not in ("propose", "superseded") for row in _rows(live[len(baseline):])):
            refuse("unsettled ledger; owner inspection required")
        versions = {cid: digest(new) for cid, (_, new) in changes.items()}
        bases = {cid: digest(old) for cid, (old, _) in changes.items()}
        if all(old == new for old, new in changes.values()):
            self.result = {"effect": "no-op", "persisted": False, "channel": "direct",
                           "operation_id": self.id, "versions": versions, "bases": bases}
            return self.result
        modes = {path: stat.S_IMODE(path.stat().st_mode) for path in before if path.exists()}
        paths = [path.relative_to(b.root).as_posix() for path in before]
        description = f"{action}: " + ", ".join(changes)
        try:
            for cid, (old, new) in changes.items():
                path = b.root / f"{cid}.md"
                if new is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    p._atomic_write(path, new)
                data = old if new is None else new
                body = frontmatter.parse(p._text(data))[1]
                p_action = "delete" if new is None else "create" if old is None else "update"
                memory.record(b, "accept", actor=actor, proposal=None, target=cid,
                              action=p_action, origin="agent-direct", channel="direct",
                              content_sha256=p.content_sha256(body),
                              base_sha256=None if old is None else bases[cid],
                              observed={k: observed[k] for k in
                                        ("by", "queries", "target_shown", "base_shown")
                                        if k in observed} if observed else None,
                              receipt={"id": self.id, "request": self.request,
                                       "operation": action, "sha256": versions[cid]})
            p.append_log(b, f"direct: {description} by {actor} ({self.id})"
                         + (f" — {' '.join(note.split())}" if note else ""))
            p._commit(b, paths, f"memory: {description}\n\nOkfy-Operation: {self.id}",
                      actor=actor, scoped=True)
            commit = head(b)
            self.result = self.verify(commit)
            if self.result["versions"] != versions or self.result["bases"] != bases:
                raise ValueError("unexpected committed receipt")
            return self.result
        except Exception:
            # A failed acknowledgement is not proof the commit failed. Never
            # move HEAD backwards. Return uncertain even on a post-commit error;
            # replay with the SAME request can verify the receipt independently.
            try:
                if head(b) == self.parent:
                    git(b, "reset", "-q", self.parent, "--", *paths)
                    for path, data in before.items():
                        if data is None:
                            path.unlink(missing_ok=True)
                        else:
                            p._atomic_write(path, data)
                            path.chmod(modes[path])
                    for d in sorted(dirs, key=lambda d: -len(d.parts)):
                        if d.exists():
                            d.rmdir()
                    self.result = {"error": "E_MEMORY_ROLLBACK", "effect": "refused",
                                   "operation_id": self.id, "message": "failed; restored before-images",
                                   "way_out": "inspect the bundle repository before retrying"}
                    return self.result
            except Exception:
                pass
            self.result = self.uncertain()
            return self.result


def _links(bundle, body, old_id, new_id, source, destination, action):
    """Rewrite only OKF's own inline link destinations; keep labels and URI text."""
    old_path = bundle.root / f"{old_id}.md"
    relocated = old_id != new_id

    def replace(match):
        target = match[1]
        if is_external(target):
            return match[0]
        cid = resolve_link(bundle, old_path, target)
        fragment = target.partition("#")[2]
        if action != "move" and fragment and (
                cid in (source, destination) or (target.startswith("#") and old_id in
                                                  (source, destination))):
            refuse(f"anchored link in {old_id}; split/merge cannot guarantee its heading")
        if not cid:
            if relocated and not target.startswith(("#", "/")):
                refuse(f"unsupported relative link in {old_id}: {target}")
            if relocated and target.endswith(".md") and not target.startswith("#"):
                refuse(f"unresolved concept link in {old_id}: {target}")
            return match[0]
        retires = action != "split" and cid == source
        if not retires and (not relocated or target.startswith("/")):
            return match[0]
        # OKF root-relative link, independent of destination depth.
        to = "/" + quote(destination if retires else cid, safe="/") + ".md"
        return match[0][:match.start(1) - match.start()] + to + (
            "#" + fragment if fragment else "") + ")"
    return LINK_RE.sub(replace, body)


def _check_retirement(bundle, docs, source):
    # These are live OKFy references, but NOT inline Markdown links. Don't
    # rewrite a decision, evaluation or a proposal on the author's behalf.
    for cid, (_, meta, _) in docs.items():
        if source in (meta.get("supersedes"), meta.get("superseded_by")):
            refuse(f"{cid} has a supersession reference to {source}")
        ev = meta.get("evidence")
        if isinstance(ev, dict) and ev.get("ref") == f"concept:{source}":
            refuse(f"{cid} cites concept:{source}")
    for row in docs.get("meta/lexicon", (None, {}, ""))[1].get("rows") or []:
        refs = row.get("maps_to") if isinstance(row, dict) else None
        if source in (refs if isinstance(refs, list) else [refs]):
            refuse(f"lexicon maps to {source}")
    purpose = bundle.purpose()
    if any(isinstance(q, str) and source in q for q in purpose.get("test_queries") or []) or any(
            isinstance(q, dict) and q.get("concept") == source
            for q in purpose.get("adversarial_queries") or []):
        refuse(f"purpose suite references {source}")
    directory = bundle.root / "proposals"
    if directory.is_symlink():
        refuse("cannot check linked proposal directory")
    if directory.exists():
        for path in directory.iterdir():
            if path.is_dir():
                refuse("cannot check a non-flat proposal queue")
            if path.suffix == ".md":
                env = bundle.load(regular_path(bundle, path)).meta["proposal"]
                if source in (env.get("target"), env.get("new_id")):
                    refuse(f"{source} is named by open proposal {path.name}")


def reorganize(bundle, *, action, source, destination, bases, actor, operation_id,
               extract=None, archetype=None):
    actor = check_actor(actor)
    with p.review_lock(bundle):
        purpose = bundle.purpose()
        if not memory_profile(purpose) or direct_policy(purpose)[1]:
            refuse("operator must select a valid evolutionary memory profile")
        if action not in ("move", "split", "merge") or source == destination:
            refuse("choose move, split or merge between distinct ordinary ids")
        if (extract is not None) != (action == "split"):
            refuse("only split takes (and requires) an exact extract")
        for cid in {source, destination, *bases}:
            ordinary_path(bundle, cid)
        op = Operation(bundle, operation_id, dict(action=action, source=source,
                       destination=destination, bases=bases, extract=extract, actor=actor))
        if op.prior:
            return op.result if "error" in op.result else {**op.result, "replayed": True}

        # Enumerate active concepts including DNA and meta, fail closed on a
        # partial scan. No generated index, proposal or foreign workspace rewrite.
        unreadable = []
        docs = {}
        for path in listing_paths(bundle, unreadable):
            cid = bundle.concept_id(path)
            try:
                raw = path.read_bytes()
                docs[cid] = (raw, *frontmatter.parse(p._text(raw)))
            except (ValueError, OSError):
                unreadable.append(cid)
        if unreadable:
            refuse(f"incomplete active concept scan: {sorted(unreadable)}")
        if source not in docs or (destination in docs) != (action == "merge"):
            refuse("source must exist; destination must exist only for merge")
        raw, sm, sb = docs[source]
        if not sb.strip():
            refuse("source must have content")
        if action != "split":
            _check_retirement(bundle, docs, source)
        basic = {"type", "title", "description", "generated", "okf_id", "evidence", "sources"}
        if action != "move" and set(sm) - basic:
            refuse("split/merge cannot redistribute source metadata without loss")
        planned = {}

        def put(cid, meta, body):
            if not body.strip():
                refuse("empty result")
            path = ordinary_path(bundle, cid)
            validate_fields(meta)
            if cid not in docs:
                p.check_new_type(bundle, meta)
            p.validate_direct_document(bundle, cid, path, meta, body, "", None, archetype)
            data = frontmatter.serialize(meta, body).encode()
            planned[cid] = (docs[cid][0] if cid in docs else None, data)

        def links(body, old, new):
            return _links(bundle, body, old, new, source, destination, action)

        if action == "split":
            if not extract.strip() or sb.count(extract) != 1:
                refuse("extract must be a nonempty unique passage from the source")
            left, right = sb.split(extract)
            if (left.strip() and not left.endswith("\n\n")) or (
                    right.strip() and not right.startswith("\n\n")) or not (left + right).strip():
                refuse("split on blank-line boundaries and leave content in the source")
            dm = minimal_metadata(destination, extract)
            dm.update({k: sm[k] for k in ("type", "sources", "evidence") if k in sm})
            dm.update(okf_id=new_id(), generated={"by": actor, "at": utc_now()})
            put(source, sm, links(left + f"[{destination}](/{destination}.md)" + right,
                                  source, source))
            put(destination, dm, links(extract, source, destination))
        else:
            relocated = links(sb, source, destination)
            planned[source] = (raw, None)
            if action == "merge":
                _, dm, db = docs[destination]
                if dm["type"] != sm["type"] or set(dm) - basic:
                    refuse("merge requires the same type and simple destination metadata")
                for key in ("sources", "evidence"):
                    if sm.get(key) != dm.get(key):
                        refuse(f"merge would conflate different {key}; reconcile explicitly")
                put(destination, dm, links(db, destination, destination).rstrip("\n")
                    + "\n\n" + relocated)
            else:
                put(destination, sm, relocated)
                if relocated == sb:
                    planned[destination] = (None, raw)  # true rename, including CRLF

        for cid, (_, meta, body) in docs.items():
            if cid in planned:
                continue
            rewritten = links(body, cid, cid)
            if rewritten != body:
                ordinary_path(bundle, cid)  # never suggest adding protected DNA to bases
                if cid not in bases:
                    refuse(f"backlink {cid} also needs its shown base, or remove the link first")
                put(cid, meta, rewritten)
        if set(bases) != set(planned):
            refuse("bases must name exactly source, destination and rewritten backlinks")
        for cid, (before, after) in planned.items():
            p._refuse_base_moved(cid, bases[cid], digest(before))
        return {**op.apply(action, actor, planned), "replayed": False}
