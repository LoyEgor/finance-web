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
def allowed_data_files(month):
    """Repo-relative data/ paths allowed to publish for `month` (YYYY-MM).

    The target month's snapshot, its transfers (bare + any dated transfers-YYYY-MM-DD),
    plus benchmarks.json and categories.json which legitimately change per snapshot.
    transfers are resolved by glob against what actually exists in finances-web/data/.
    """
    files = [
        f"data/{month}.json",
        "data/benchmarks.json",
        "data/categories.json",
    ]
    # Precise match: exactly transfers-{month}.json or transfers-{month}-DD.json.
    # A loose startswith over-matched siblings like transfers-2026-061.json.
    data_dir = os.path.join(PUBLIC_REPO, "data")
    tre = re.compile(rf"^transfers-{re.escape(month)}(-\d{{2}})?\.json$")
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            if tre.match(name):
                files.append(f"data/{name}")
    # Deduplicate while preserving order.
    seen = set()
    out = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# ── sync ─────────────────────────────────────────────────────────────────────
def plan_sync(month):
    """Return [(rel_path, src_abs, dst_abs, status)] for the allowed files.

    status: "new" (absent in private), "changed" (differs), "same" (identical),
    "missing-src" (allowed but not present in finances-web — skipped, not an error
    for the per-snapshot benchmarks/categories case is unlikely, but a missing
    snapshot is reported so the user notices).
    """
    plan = []
    for rel in allowed_data_files(month):
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

    Handles renames (`R  old -> new`, take new) and quoted/space paths. We only
    care about which paths changed, not the XY status codes.
    """
    paths = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        # Format: XY<space>PATH  (or  XY<space>OLD -> NEW for renames/copies)
        rest = line[3:] if len(line) > 3 else line.strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip()
        if rest.startswith('"') and rest.endswith('"'):
            rest = rest[1:-1]
        paths.append(rest)
    return paths


def guard(month):
    """Inspect the private repo's data/ working tree; return (ok, changed, stray).

    changed = all paths git reports dirty under data/.
    stray   = those NOT in the allowlist for `month`. Non-empty stray => abort.
    """
    allow = set(allowed_data_files(month))
    # -uall expands untracked directories to individual files so a whole new dir
    # never collapses to one `data/` entry that hides the actual offending paths.
    porcelain = git_out(PRIVATE_REPO, "status", "--porcelain", "-uall", "--", "data/")
    changed = parse_porcelain_paths(porcelain)
    stray = [p for p in changed if p not in allow]
    return (len(stray) == 0, changed, stray)


# ── commit/push ──────────────────────────────────────────────────────────────
COMMIT_MSG_PRIVATE = "data: {month} snapshot"
COMMIT_MSG_PUBLIC = "tooling/skill update"


def private_commit_push(month, paths, do_it):
    """Stage ONLY `paths`, commit, push origin. Returns a human summary string."""
    if not paths:
        return "PRIVATE: nothing to commit (working tree clean for allowed files)."
    msg = COMMIT_MSG_PRIVATE.format(month=month)
    if not do_it:
        return (
            "PRIVATE would: git add {n} file(s), "
            'commit -m "{msg}", push origin\n    + '.format(n=len(paths), msg=msg)
            + "\n    + ".join(paths)
        )
    git(PRIVATE_REPO, "add", "--", *paths)
    git(PRIVATE_REPO, "commit", "-m", msg)
    head = git_out(PRIVATE_REPO, "rev-parse", "--short", "HEAD").strip()
    git(PRIVATE_REPO, "push", "origin")
    return f'PRIVATE: committed {head} "{msg}" ({len(paths)} file(s)) and pushed.'


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
    # config.example.py must pass before the generic tools/*.py rule, and the
    # real config.py is already denied above.
    if rel == "tools/config.example.py" or rel == "tools/.env.example":
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
    git(PUBLIC_REPO, "add", "--", *allowed)
    git(PUBLIC_REPO, "commit", "-m", COMMIT_MSG_PUBLIC)
    head = git_out(PUBLIC_REPO, "rev-parse", "--short", "HEAD").strip()
    git(PUBLIC_REPO, "push", "origin")
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

    plan = plan_sync(month)

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
        ok, changed, stray = guard(month)
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
        allow_data = set(allowed_data_files(month))
        would_copy = [r for r, _s, _d, st in plan
                      if st in ("new", "changed") and r in allow_data]
        print(private_commit_push(month, would_copy, do_it=False))
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
    copied = do_copy(plan)
    if not copied:
        print("No new/changed data files to sync; checking guard and public repo anyway.")

    ok, changed, stray = guard(month)
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

    # Stage only files THIS run copied that are also on the data allowlist.
    allow_data = set(allowed_data_files(month))
    to_stage = [r for r in copied if r in allow_data]

    # Confirm before any git write — print the actual file PATHS, not just counts.
    print("About to commit & push:")
    print(f'  PRIVATE: "{COMMIT_MSG_PRIVATE.format(month=month)}"')
    if to_stage:
        for p in to_stage:
            print(f"    + {p}")
    else:
        print("    (no new/changed data files to stage)")
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
        print("Aborted by user. Synced files remain on disk; no git writes performed.")
        return 1

    print("")
    print(private_commit_push(month, to_stage, do_it=True))
    pub_ok, pub_msg = public_commit_push(do_it=True)
    print(pub_msg)

    # After the commits: the month is now verified-and-published, which is exactly
    # the signal every downstream consumer waits for.
    if cplan:
        print("")
        print("CONSUMERS:")
        for ln in sync_consumers(month, cplan):
            print(ln)

    if not pub_ok:
        return 1
    print("")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
