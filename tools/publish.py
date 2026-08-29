#!/usr/bin/env python3
"""
publish.py — push a monthly snapshot to the PRIVATE data repo and the code to the
PUBLIC repo, then fan the snapshot out to every downstream consumer, with a hard
guard against touching any month but the target one.

Two repos (see config.PRIVATE_DATA_REPO and this file's own location):
  PUBLIC  = this finances-web checkout (the public GitHub Pages site).
            data/*.json is gitignored here (only categories.json is tracked); we
            commit only the code/tooling files this run touched — the data and any
            private config stay out by both .gitignore AND an explicit allowlist.
  PRIVATE = config.PRIVATE_DATA_REPO (your private data repo). It TRACKS all
            data/*.json and is the source the running app fetches. publish.py
            copies ONLY the target month's allowed files here, then commits/pushes.

The GUARD is the point of this script: a skill misfire could write a stray month
into finances-web/data/ and a naive sync would clobber other months in the
private repo. So after syncing we diff the private repo's data/ via git and ABORT
unless every changed path is in the explicit allowlist for the target month.

CONSUMERS = config.SNAPSHOT_CONSUMERS — projects whose source of truth is this
            snapshot (e.g. an analysis pipeline reading the book). They are synced
            AFTER the commits, because committing is the owner's signal that the
            numbers are verified. Files are copied and each consumer's refresh
            command is run, but nothing is ever committed there: consumers are
            shared checkouts that routinely hold other agents' uncommitted work.

DEFAULT = dry run: fully read-only (no file writes, no git writes) — prints the
target month, the files that would sync, a content diff of what would change in
the private repo, the guard result, and what each repo would commit/push.
`--push` actually performs it, but first prints the diff and asks `Proceed? [y/N]`,
continuing only on 'y'. Running --push and confirming IS the authorization.

Stdlib only; git is driven via subprocess (no git library).
"""

import argparse
import datetime
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

# This finances-web checkout (PUBLIC repo) — tools/ lives one level under root.
PUBLIC_REPO = os.path.dirname(HERE)
PRIVATE_REPO = config.PRIVATE_DATA_REPO

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


# ── git helpers (subprocess only) ─────────────────────────────────────────────
def git(repo, *args, check=True):
    """Run `git -C repo <args>`, return CompletedProcess (stdout/stderr captured)."""
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def git_out(repo, *args):
    return git(repo, *args).stdout


# ── allowlist ────────────────────────────────────────────────────────────────
TRANSFERS_RE = re.compile(r"^transfers-(\d{4}-\d{2}(?:-\d{2})?)\.json$")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _snapshot_months():
    """Month ids with a snapshot file in finances-web/data/, ascending."""
    data_dir = os.path.join(PUBLIC_REPO, "data")
    if not os.path.isdir(data_dir):
        return []
    return sorted(n[:-5] for n in os.listdir(data_dir)
                  if n.endswith(".json") and MONTH_RE.match(n[:-5]))


def _snapshot_date(month):
    d = _read_json(os.path.join(PUBLIC_REPO, "data", f"{month}.json"))
    return ((d or {}).get("meta") or {}).get("date")


def _transfers_date(path, name):
    """The date a transfers group belongs to — meta.date, else the filename's month
    id. Same rule as reconcile.load_groups, so publish selects the set the
    reconciliation actually used."""
    d = _read_json(path)
    meta = (d.get("meta") or {}) if isinstance(d, dict) else {}
    if meta.get("date"):
        return meta["date"]
    m = TRANSFERS_RE.match(name)
    return m.group(1)[:7] if m else None


def _selects_transfers(name, path, month):
    """Does the transfers file `name` belong to `month`'s publish set?

    By filename month, OR by its flow date falling in the reconciliation period
    (prev_snapshot_date, curr_snapshot_date] — a transfers file is named for the
    FLOW's date, and that period routinely starts in the previous calendar month.
    Selecting by filename alone leaves those flows unpublished in either month and
    the app reports them as market return. The SAME rule decides what to copy and
    what to delete, or a re-dated file is published under its new name and left
    behind under the old one.
    """
    # Precise match: exactly transfers-{month}.json or transfers-{month}-DD.json.
    # A loose startswith over-matched siblings like transfers-2026-061.json.
    if re.match(rf"^transfers-{re.escape(month)}(-\d{{2}})?\.json$", name):
        return True
    if not TRANSFERS_RE.match(name):
        return False
    curr_date = _snapshot_date(month)
    if not curr_date:
        return False
    priors = [m for m in _snapshot_months() if m < month]
    prev_date = _snapshot_date(priors[-1]) if priors else None
    d = _transfers_date(path, name)
    return bool(d and d <= curr_date and (prev_date is None or d > prev_date))


def allowed_data_files(month, deletes=None):
    """Repo-relative data/ paths allowed to publish for `month` (YYYY-MM).

    The target month's snapshot, its transfers (see _selects_transfers), plus
    benchmarks.json and categories.json which legitimately change per snapshot.

    `deletes` is the delete plan (see plan_deletes), allowlisted so staging a removal
    does not read as a stray change. It MUST be the plan computed before any file was
    removed: re-deriving it here after do_deletes ran returns an empty list, and the
    ` D data/...` lines git then reports would abort the run as strays.
    """
    files = [
        f"data/{month}.json",
        "data/benchmarks.json",
        "data/categories.json",
    ]
    data_dir = os.path.join(PUBLIC_REPO, "data")
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            if _selects_transfers(name, os.path.join(data_dir, name), month):
                files.append(f"data/{name}")
    files += plan_deletes(month) if deletes is None else list(deletes)
    # Deduplicate while preserving order.
    seen = set()
    out = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# ── sync ─────────────────────────────────────────────────────────────────────
def private_tracked_data():
    """data/ paths git TRACKS in the private repo."""
    out = git(PRIVATE_REPO, "ls-files", "--", "data/", check=False).stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def plan_deletes(month):
    """Transfers files this month's publish selects that are present in the PRIVATE
    repo but gone from the source. plan_sync only enumerates the source, so a file
    that was re-dated, consolidated or corrected away stays behind — and the app
    prefix-matches every transfers-{month}*, so the stale copy is summed a SECOND
    time. Scoped by _selects_transfers, the same rule the copy set uses: never
    another month's files, and never blind to a file the interval owns but the
    filename does not name.

    Candidates come from the private working tree AND from git's index, so the plan
    survives being consulted after the files were already removed from disk.
    """
    src_dir = os.path.join(PUBLIC_REPO, "data")
    dst_dir = os.path.join(PRIVATE_REPO, "data")
    if not os.path.isdir(dst_dir):
        return []
    names = {n for n in os.listdir(dst_dir)}
    names |= {rel.split("/", 1)[1] for rel in private_tracked_data()
              if rel.startswith("data/") and "/" not in rel.split("/", 1)[1]}
    return [f"data/{n}" for n in sorted(names)
            if _selects_transfers(n, os.path.join(dst_dir, n), month)
            and not os.path.exists(os.path.join(src_dir, n))]


def do_deletes(deletes):
    """Remove the stale private files. Returns the rels actually removed."""
    gone = []
    for rel in deletes:
        path = os.path.join(PRIVATE_REPO, rel)
        if os.path.exists(path):
            os.remove(path)
            gone.append(rel)
    return gone


def plan_sync(month, deletes=None):
    """Return [(rel_path, src_abs, dst_abs, status)] for the allowed files.

    status: "new" (absent in private), "changed" (differs), "same" (identical),
    "missing-src" (allowed but not present in finances-web — skipped, not an error
    for the per-snapshot benchmarks/categories case is unlikely, but a missing
    snapshot is reported so the user notices).
    """
    plan = []
    deletes = set(plan_deletes(month) if deletes is None else deletes)
    for rel in allowed_data_files(month, deletes):
        if rel in deletes:
            continue  # reported separately by plan_deletes; absent from the source by definition
        src = os.path.join(PUBLIC_REPO, rel)
        dst = os.path.join(PRIVATE_REPO, rel)
        if not os.path.exists(src):
            plan.append((rel, src, dst, "missing-src"))
            continue
        if not os.path.exists(dst):
            status = "new"
        elif filecmp.cmp(src, dst, shallow=False):
            status = "same"
        else:
            status = "changed"
        plan.append((rel, src, dst, status))
    return plan


def content_diff(rel, src, dst):
    """Unified diff of a single file's current private content vs the incoming src.

    Uses difflib (stdlib) so it works even before the file is copied, against the
    on-disk private file (not git), which is what the sync will actually overwrite.
    """
    import difflib

    new_lines = _read_lines(src)
    old_lines = _read_lines(dst) if os.path.exists(dst) else []
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"private/{rel}", tofile=f"incoming/{rel}",
        lineterm="",
    )
    return list(diff)


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def do_copy(plan):
    """Copy src->dst for every new/changed entry. Returns list of copied rels."""
    copied = []
    for rel, src, dst, status in plan:
        if status in ("new", "changed"):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    return copied


# ── downstream consumers ─────────────────────────────────────────────────────
def consumers():
    return getattr(config, "SNAPSHOT_CONSUMERS", []) or []


def plan_consumers(month):
    """Return [(spec, src, dst, status)] for each config.SNAPSHOT_CONSUMERS entry.

    status mirrors plan_sync's vocabulary ("new"/"changed"/"same") plus
    "missing-src" and "missing-repo" so a moved/renamed consumer surfaces as a
    reported skip instead of a silent no-op.
    """
    src = os.path.join(PUBLIC_REPO, "data", f"{month}.json")
    plan = []
    for spec in consumers():
        dst = os.path.join(spec["path"], spec["inbox"], f"{month}.json")
        if not os.path.isdir(spec["path"]):
            status = "missing-repo"
        elif not os.path.exists(src):
            status = "missing-src"
        elif not os.path.exists(dst):
            status = "new"
        elif filecmp.cmp(src, dst, shallow=False):
            status = "same"
        else:
            status = "changed"
        plan.append((spec, src, dst, status))
    return plan


def sync_consumers(month, plan):
    """Copy the snapshot into each consumer inbox and run its refresh command.

    Returns human-readable report lines. A consumer failure is REPORTED, never
    fatal: the private commit already succeeded by this point, so aborting would
    leave the published month half-delivered with no way to retry just this step.
    """
    lines = []
    for spec, src, dst, status in plan:
        name = spec["name"]
        if status in ("missing-repo", "missing-src"):
            lines.append(f"  [{status}] {name}: skipped ({spec['path']})")
            continue
        if status != "same":
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        lines.append(f"  [{'up-to-date' if status == 'same' else status}] "
                     f"{name}: {spec['inbox']}/{month}.json")

        refresh = spec.get("refresh")
        if refresh:
            try:
                r = subprocess.run(refresh, cwd=spec["path"], capture_output=True,
                                   text=True, timeout=600)
            except (OSError, subprocess.SubprocessError) as e:
                lines.append(f"    refresh FAILED ({' '.join(refresh)}): {e}")
            else:
                tail = (r.stdout or r.stderr or "").strip().splitlines()
                note = tail[-1] if tail else "(no output)"
                verb = "refresh" if r.returncode == 0 else f"refresh FAILED rc={r.returncode}"
                lines.append(f"    {verb}: {note}")

        watched = [spec["inbox"], *spec.get("derived", [])]
        # Unstripped: porcelain's XY status column is 2 chars, so a leading-space
        # status (" M path") loses its first path character if the block is stripped.
        # check=False: a consumer need not be a git repo, and the files are already
        # delivered by now — an unavailable git must not raise past a done sync.
        dirty = git(spec["path"], "status", "--porcelain", "-uall", "--", *watched,
                    check=False).stdout
        if dirty.strip():
            lines.append(f"    uncommitted in {name} (commit it there yourself):")
            for p in parse_porcelain_paths(dirty):
                lines.append(f"      ~ {p}")
    return lines


# ── guard ────────────────────────────────────────────────────────────────────
def parse_porcelain_paths(porcelain):
    """Yield repo-relative paths from `git status --porcelain` output.

    A rename/copy line (`R  old -> new`) yields BOTH sides: the source is a DELETION
    the allowlist must judge too, and keeping only the destination hid e.g.
    `R portfolio.md -> README-notes.md` from the public guard. Quoted/space paths are
    unquoted. We only care about which paths changed, not the XY status codes.
    """
    paths = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        # Format: XY<space>PATH  (or  XY<space>OLD -> NEW for renames/copies)
        rest = line[3:] if len(line) > 3 else line.strip()
        for side in (rest.split(" -> ", 1) if " -> " in rest else [rest]):
            side = side.strip()
            if side.startswith('"') and side.endswith('"'):
                side = side[1:-1]
            if side:
                paths.append(side)
    return paths


def guard(month, deletes=None):
    """Inspect the private repo's data/ working tree; return (ok, changed, stray).

    changed = all paths git reports dirty under data/.
    stray   = those NOT in the allowlist for `month`. Non-empty stray => abort.

    `deletes` must be the plan captured BEFORE any removal — see allowed_data_files.
    """
    allow = set(allowed_data_files(month, deletes))
    # -uall expands untracked directories to individual files so a whole new dir
    # never collapses to one `data/` entry that hides the actual offending paths.
    porcelain = git_out(PRIVATE_REPO, "status", "--porcelain", "-uall", "--", "data/")
    changed = parse_porcelain_paths(porcelain)
    stray = [p for p in changed if p not in allow]
    return (len(stray) == 0, changed, stray)


# ── commit/push ──────────────────────────────────────────────────────────────
COMMIT_MSG_PRIVATE = "data: {month} snapshot"
COMMIT_MSG_PUBLIC = "tooling/skill update"


def _git_failure(label, e):
    """A rejected push or hook must not escape as a bare traceback: the caller has to
    report the half-delivered state and exit non-zero so the retry path is obvious."""
    return f"{label} FAILED: {' '.join(e.cmd)}\n    " + ((e.stderr or e.stdout or "").strip() or "(no stderr)")


def private_commit_push(month, paths, do_it):
    """Stage ONLY `paths`, commit ONLY them, push every unpushed commit. (ok, summary).

    "Nothing to commit" is NOT "nothing to push": when an earlier run committed and
    then failed to push, the files are committed, `paths` comes back empty, and a
    retry that returned early would report success while the month sits unpushed. So
    the push is decided by the gap to the upstream, never by what this run staged.
    """
    msg = COMMIT_MSG_PRIVATE.format(month=month)
    if not do_it:
        if not paths:
            return True, "PRIVATE: nothing to commit (working tree clean for allowed files)."
        return True, (
            "PRIVATE would: git add {n} file(s), "
            'commit --only -m "{msg}", push origin\n    + '.format(n=len(paths), msg=msg)
            + "\n    + ".join(paths)
        )
    try:
        if paths:
            git(PRIVATE_REPO, "add", "--all", "--", *paths)
            # --only + pathspec: a bare `git commit` also publishes whatever the operator
            # had already staged in the private repo, which the guard never inspects.
            git(PRIVATE_REPO, "commit", "--only", "-m", msg, "--", *paths)
            head = git_out(PRIVATE_REPO, "rev-parse", "--short", "HEAD").strip()
            done = f'PRIVATE: committed {head} "{msg}" ({len(paths)} file(s))'
        else:
            done = "PRIVATE: nothing to commit (working tree clean for allowed files)"
        r = git(PRIVATE_REPO, "rev-list", "--count", "@{u}..HEAD", check=False)
        if r.returncode != 0:
            return False, (f"{done}, but the upstream (@{{u}}) does not resolve, so the push cannot be "
                           f"verified — set a tracking branch and push by hand.\n    "
                           + ((r.stderr or "").strip() or "(no stderr)"))
        ahead = int(r.stdout.strip() or 0)
        if ahead:
            git(PRIVATE_REPO, "push", "origin")
    except subprocess.CalledProcessError as e:
        return False, _git_failure("PRIVATE", e)
    return True, (f"{done}; pushed {ahead} commit(s)." if ahead
                  else f"{done}; already up to date with origin.")


# ── PUBLIC-side code allowlist (defense-in-depth over .gitignore) ──────────────
# Only these code/tooling paths may ever be staged in the public repo. Anything
# else dirty (a stray data money file, portfolio.md, img/, tools/.env,
# tools/config.py, tools/out/, __pycache__, …) => abort. config.example.py is the
# only tools/config* allowed; the real tools/config.py is explicitly denied even
# though .gitignore should already exclude it.
PUBLIC_DENY_PREFIXES = (
    "tools/out/",
    "data/",          # all data money files (categories.json re-allowed below)
    "img/",
)
PUBLIC_DENY_EXACT = {
    "tools/config.py",
    "tools/.env",
    "portfolio.md",
}


def public_path_allowed(rel):
    """True iff a public-repo-relative path is on the code allowlist."""
    if rel in PUBLIC_DENY_EXACT:
        return False
    if any(rel.startswith(p) for p in PUBLIC_DENY_PREFIXES):
        # data/categories.json is the one tracked data file.
        return rel == "data/categories.json"
    if "__pycache__/" in rel or rel.endswith(".pyc"):
        return False
    # ANY tools/config*.py but the template holds real venues, amounts and the
    # private repo path — `cp config.py config_local.py` before an experiment leaves
    # an untracked copy that -uall surfaces and the generic tools/*.py rule allows.
    base_py = rel.rsplit("/", 1)[-1]
    if rel.startswith("tools/") and base_py.startswith("config") and base_py.endswith(".py"):
        return rel == "tools/config.example.py"
    if rel == "tools/.env.example":
        return True
    if rel.startswith("tools/") and rel.endswith(".py"):
        return True
    if rel.startswith(".claude/skills/"):
        return True
    if rel == ".gitignore":
        return True
    if rel.startswith(("css/", "js/", "icons/")):
        return True
    if rel in ("manifest.json", "index.html"):
        return True
    base = rel.rsplit("/", 1)[-1]
    if base.startswith("README"):
        return True
    return False


def public_has_changes():
    return bool(git_out(PUBLIC_REPO, "status", "--porcelain", "-uall").strip())


def public_partition():
    """Split the public repo's dirty paths into (allowed, denied).

    -uall expands untracked directories to individual files; otherwise a whole new
    dir (e.g. tools/ before any tool is tracked) collapses to one `tools/` entry,
    which the allowlist can't reason about file-by-file.
    """
    changed = parse_porcelain_paths(git_out(PUBLIC_REPO, "status", "--porcelain", "-uall"))
    allowed = [p for p in changed if public_path_allowed(p)]
    denied = [p for p in changed if not public_path_allowed(p)]
    return allowed, denied


def public_commit_push(do_it):
    """Stage ONLY allowlisted code/tooling paths, commit, push.

    ABORTS (returns ok=False) if any dirty path falls outside the code allowlist —
    defense-in-depth so a misconfigured .gitignore can't leak a data/private file.
    Returns (ok, summary).
    """
    if not public_has_changes():
        return True, "PUBLIC: nothing to commit (skip)."
    allowed, denied = public_partition()
    if denied:
        return False, (
            "PUBLIC ABORT: dirty paths OUTSIDE the code allowlist (would leak "
            "private/data files). Offending paths:\n    ! "
            + "\n    ! ".join(denied)
        )
    if not allowed:
        return True, "PUBLIC: nothing to commit (no allowlisted code changes)."
    if not do_it:
        return True, (
            'PUBLIC would: git add (allowlisted only), commit -m "{msg}", push origin\n    ~ '.format(
                msg=COMMIT_MSG_PUBLIC
            )
            + "\n    ~ ".join(allowed)
        )
    try:
        git(PUBLIC_REPO, "add", "--all", "--", *allowed)
        git(PUBLIC_REPO, "commit", "--only", "-m", COMMIT_MSG_PUBLIC, "--", *allowed)
        head = git_out(PUBLIC_REPO, "rev-parse", "--short", "HEAD").strip()
        git(PUBLIC_REPO, "push", "origin")
    except subprocess.CalledProcessError as e:
        return False, _git_failure("PUBLIC", e)
    return True, f'PUBLIC: committed {head} "{COMMIT_MSG_PUBLIC}" ({len(allowed)} file(s)) and pushed.'


# ── reporting ────────────────────────────────────────────────────────────────
def print_plan_and_diff(month, plan):
    print(f"Target month: {month}")
    print(f"PUBLIC  repo: {PUBLIC_REPO}")
    print(f"PRIVATE repo: {PRIVATE_REPO}")
    print("")
    print("Allowed files to sync (finances-web/data -> private/data):")
    for rel, _src, _dst, status in plan:
        print(f"  [{status:>11}] {rel}")
    print("")

    diff_targets = [(r, s, d) for r, s, d, st in plan if st in ("new", "changed")]
    if not diff_targets:
        print("Content diff: (no new/changed files — private data already matches)")
        return
    print("Content diff (private current -> incoming):")
    for rel, src, dst in diff_targets:
        lines = content_diff(rel, src, dst)
        if not lines:
            continue
        print(f"  --- {rel} ---")
        for ln in lines:
            print(f"    {ln}")
    print("")


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="Publish a monthly snapshot to the private data repo and code to the public repo.")
    ap.add_argument("--month", help="Target month YYYY-MM (default: current calendar month).")
    ap.add_argument("--push", action="store_true", help="Actually sync/commit/push (default is a read-only dry run).")
    args = ap.parse_args(argv)

    month = args.month or datetime.date.today().strftime("%Y-%m")
    if not MONTH_RE.match(month):
        print(f"ERROR: --month must be YYYY-MM, got: {month!r}", file=sys.stderr)
        return 2

    if not os.path.isdir(os.path.join(PRIVATE_REPO, ".git")):
        print(f"ERROR: PRIVATE_DATA_REPO is not a git repo: {PRIVATE_REPO}", file=sys.stderr)
        return 2

    # ONE delete plan for the whole run, captured before anything is touched: every
    # later consumer (allowlist, guard, staging) gets this list, never a re-derivation
    # against a tree the deletions already changed.
    deletes = plan_deletes(month)
    plan = plan_sync(month, deletes)

    snapshot_entry = next((p for p in plan if p[0] == f"data/{month}.json"), None)
    if snapshot_entry and snapshot_entry[3] == "missing-src":
        print(f"ERROR: snapshot data/{month}.json not found in finances-web/data/ — nothing to publish.", file=sys.stderr)
        return 2

    print("=" * 72)
    print("PUBLISH — DRY RUN" if not args.push else "PUBLISH — LIVE (--push)")
    print("=" * 72)
    print_plan_and_diff(month, plan)

    if not args.push:
        # Dry run: simulate the guard against the would-be private state without
        # writing. We can't run git diff on uncopied files, so we predict the
        # guard from the plan: any new/changed/same allowed file is in-allowlist
        # by construction, plus we surface any ALREADY-dirty stray paths in the
        # private repo so the user sees a pre-existing problem before --push.
        if deletes:
            print("Stale private files that would be DELETED (absent from the source; the app "
                  "would otherwise sum them twice):")
            for rel in deletes:
                print(f"  [     delete] {rel}")
            print("")
        ok, changed, stray = guard(month, deletes)
        print("Guard (current private working tree under data/):")
        if not changed:
            print("  clean — no pre-existing changes in private data/.")
        else:
            for p in changed:
                tag = "STRAY!" if p in stray else "ok"
                print(f"  [{tag}] {p}")
        if stray:
            print("  WARNING: private data/ already has out-of-allowlist changes above;")
            print("           resolve them before running --push or the guard will abort.")
        print("")
        # What WOULD be committed/pushed. Stage only files this run would copy
        # that are also on the data allowlist (defense-in-depth vs. dirty siblings).
        allow_data = set(allowed_data_files(month, deletes))
        would_copy = [r for r, _s, _d, st in plan
                      if st in ("new", "changed") and r in allow_data] + deletes
        _priv_ok, priv_msg = private_commit_push(month, would_copy, do_it=False)
        print(priv_msg)
        print("")
        pub_ok, pub_msg = public_commit_push(do_it=False)
        print(pub_msg)
        if not pub_ok:
            print("")
            print("DRY RUN: public guard WOULD abort — resolve the offending paths above "
                  "before --push.")
        cplan = plan_consumers(month)
        if cplan:
            print("")
            print(f"CONSUMERS would receive data/{month}.json:")
            for spec, _src, _dst, status in cplan:
                refresh = spec.get("refresh")
                suffix = f" + refresh: {' '.join(refresh)}" if refresh else ""
                print(f"  [{status:>11}] {spec['name']}: {spec['inbox']}/{month}.json{suffix}")
        print("")
        print("DRY RUN complete — no files written, no git writes. Re-run with --push to apply.")
        return 0

    # ── LIVE path ────────────────────────────────────────────────────────────
    # Copies only. A copy is idempotent and re-derivable from the source, so it is safe
    # to make before the prompt; a private-only file, once deleted, is not — the
    # deletions wait until the operator has actually confirmed them below.
    copied = do_copy(plan)
    if not copied and not deletes:
        print("No new/changed data files to sync; checking guard and public repo anyway.")

    ok, changed, stray = guard(month, deletes)
    print("Guard (private working tree under data/ after sync):")
    for p in changed:
        tag = "STRAY!" if p in stray else "ok"
        print(f"  [{tag}] {p}")
    if not ok:
        print("")
        print("ABORT: private repo has changes OUTSIDE the allowlist for "
              f"{month}. Refusing to commit. Offending paths:", file=sys.stderr)
        for p in stray:
            print(f"  {p}", file=sys.stderr)
        print("No commit/push performed. Inspect the private repo; the synced "
              "files were copied but nothing was staged.", file=sys.stderr)
        return 1
    print("  guard OK — all changes within the allowlist.")
    print("")

    # Public-side allowlist guard (defense-in-depth) BEFORE any prompt: abort now
    # if a non-code path is dirty in the public repo, rather than after committing
    # the private side.
    pub_allowed, pub_denied = public_partition() if public_has_changes() else ([], [])
    if pub_denied:
        print("ABORT: public repo has dirty paths OUTSIDE the code allowlist. "
              "Refusing to commit anything. Offending paths:", file=sys.stderr)
        for p in pub_denied:
            print(f"  {p}", file=sys.stderr)
        print("No commit/push performed on either repo.", file=sys.stderr)
        return 1

    # Stage every DIRTY allowlisted path, not just what this run copied: an aborted
    # earlier run leaves the files copied but uncommitted, and `copied` is then empty
    # while the month is still unpublished.
    allow_data = set(allowed_data_files(month, deletes))
    to_stage = sorted(set(changed) & allow_data)

    # Confirm before any git write — print the actual file PATHS, not just counts.
    print("About to commit & push:")
    print(f'  PRIVATE: "{COMMIT_MSG_PRIVATE.format(month=month)}"')
    if to_stage:
        for p in to_stage:
            print(f"    + {p}")
    elif not deletes:
        print("    (no new/changed data files to stage)")
    for p in deletes:
        print(f"    - {p}  (stale in private, gone from the source)")
    print(f'  PUBLIC:  "{COMMIT_MSG_PUBLIC}"')
    if pub_allowed:
        for p in pub_allowed:
            print(f"    ~ {p}")
    else:
        print("    (no allowlisted code changes)")
    cplan = plan_consumers(month)
    for spec, _src, _dst, status in cplan:
        print(f"  CONSUMER {spec['name']}: [{status}] {spec['inbox']}/{month}.json "
              f"(sync only, not committed there)")
    reply = input("Proceed? [y/N] ").strip().lower()
    if reply != "y":
        print("Aborted by user. Synced files remain on disk; nothing was deleted and "
              "no git writes were performed.")
        return 1

    print("")
    # Only TRACKED removals can be staged: `git add` on a path that is neither on disk
    # nor in the index fails the whole commit.
    tracked = private_tracked_data()
    removed = do_deletes(deletes)
    for rel in removed:
        print(f"Removed stale private file (absent from the source): {rel}")
    to_stage = sorted(set(to_stage) | (set(removed) & tracked))

    priv_ok, priv_msg = private_commit_push(month, to_stage, do_it=True)
    print(priv_msg)
    pub_ok, pub_msg = public_commit_push(do_it=True) if priv_ok else (
        False, "PUBLIC: skipped — the private commit/push failed above.")
    print(pub_msg)

    # After the commits: the month is now verified-and-published, which is exactly
    # the signal every downstream consumer waits for. A failed private push means it
    # is NOT published, so consumers must not be handed the numbers yet.
    if cplan and priv_ok:
        print("")
        print("CONSUMERS:")
        for ln in sync_consumers(month, cplan):
            print(ln)

    if not pub_ok or not priv_ok:
        return 1
    print("")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
