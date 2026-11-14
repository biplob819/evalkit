#!/usr/bin/env python3
"""Fail when a Node's source changed without a `code_version` bump (RUN-07).

Usage:
    uv run python scripts/check_code_versions.py            # check
    uv run python scripts/check_code_versions.py --update   # rewrite the manifest after a bump
    uv run python scripts/check_code_versions.py --rehash   # only after HASH_SCHEME changes

`--rehash` re-records every hash under a new hashing scheme (e.g. when non-output methods
were excluded). It refuses if any node's code_version differs from the manifest, so it can
never be used to slip a behaviour change past the guard together with a version change.
"""

from __future__ import annotations

import argparse
import sys

from evalkit.runtime.versions import (
    BUMP_HINT,
    HASH_SCHEME,
    build_manifest,
    check,
    load_manifest,
    node_classes,
    package_modules,
    write_manifest,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--rehash", action="store_true")
    args = ap.parse_args(argv)
    classes = node_classes(*package_modules())
    if args.rehash:
        manifest = load_manifest()
        fresh = build_manifest(classes)
        moved = [
            n
            for n in fresh
            if n in manifest and fresh[n]["code_version"] != manifest[n]["code_version"]
        ]
        if moved or set(fresh) != set(manifest):
            print("refusing to rehash: node set or code_version changed; use --update")
            return 1
        write_manifest(fresh)
        print(f"manifest re-hashed under scheme {HASH_SCHEME} ({len(fresh)} nodes)")
        return 0
    if args.update:
        manifest = load_manifest()
        problems = check(manifest, classes)
        stale = [
            p for p in problems if BUMP_HINT in p or "needs non-empty" in p or "also used by" in p
        ]
        if stale:
            print("refusing to update: bump code_version first:")
            for p in stale:
                print(f"  - {p}")
            return 1
        write_manifest(build_manifest(classes))
        print(f"manifest updated ({len(classes)} nodes)")
        return 0
    problems = check(load_manifest(), classes)
    if problems:
        print("code versions: FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"code versions: OK ({len(classes)} nodes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
