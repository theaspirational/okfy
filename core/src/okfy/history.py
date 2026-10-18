"""Who changed a concept file, when, and what an agent missed — read from
git, which already holds every version and, since commits name their writer
(author and the `Okfy-Actor` trailer), who wrote each one. No new state:
nothing here is recorded, only read back."""
import hashlib

from okfy.gitenv import run_git


def _relpath(bundle, concept_id: str) -> str:
    return f"{concept_id}.md"


def last_change(bundle, concept_id: str) -> dict:
    """`{changed_by, changed_at, commit}` of the last commit touching the
    concept's file — the `Okfy-Actor` trailer when the commit carries one,
    else the author. `{}` without git or without history."""
    if not (bundle.root / ".git").exists():
        return {}
    r = run_git(bundle.root, "log", "-1",
                "--format=%H%x00%an%x00%aI%x00%(trailers:key=Okfy-Actor,valueonly)",
                "--", _relpath(bundle, concept_id), capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    commit, author, at, trailer = (r.stdout.strip("\n").split("\x00") + [""] * 4)[:4]
    return {"changed_by": trailer.strip() or author, "changed_at": at, "commit": commit}


def version_with_sha256(bundle, concept_id: str, sha256: str) -> str | None:
    """The newest commit at which the concept's file hashed to `sha256`
    (the digest okfy_show hands out), or None when no committed version did.
    # ponytail: hashes every past version of one file; a concept is small and
    # its history short, so this stays cheap until someone measures otherwise."""
    if not (bundle.root / ".git").exists():
        return None
    rel = _relpath(bundle, concept_id)
    r = run_git(bundle.root, "log", "--format=%H", "--", rel,
                capture_output=True, text=True)
    if r.returncode != 0:
        return None
    for commit in r.stdout.split():
        blob = run_git(bundle.root, "show", f"{commit}:{rel}", capture_output=True)
        if blob.returncode == 0 and hashlib.sha256(blob.stdout).hexdigest() == sha256:
            return commit
    return None


def diff_from(bundle, concept_id: str, commit: str) -> str:
    """Unified diff of the concept's file from `commit` to what is on disk now."""
    r = run_git(bundle.root, "diff", "--no-color", "--no-ext-diff", commit, "--",
                _relpath(bundle, concept_id), capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""
