"""Standalone test for db.get_deployed_app_for_subapp.

Run from the repo root:
    cd /opt/ojas && python3 server/test_subapp_uniqueness.py

Exits 0 on success, 1 on any assertion failure. Touches the live
deployed_apps table (inserts 2 rows, deletes them at the end) — safe
to run against the prod DB because the test slugs are unique and
get cleaned up.
"""
import sys
sys.path.insert(0, "/opt/ojas/server")

from db import (
    get_deployed_app_for_subapp,
    allocate_deployed_slug,
    create_deployed_app,
    delete_deployed_app,
)

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


slug_a = allocate_deployed_slug("test-a")
create_deployed_app(
    slug=slug_a, name="test-a",
    source_session_id="sess-1", source_project_id="proj-1",
    owner_user_id="u-1", app_dir=f"/tmp/{slug_a}",
    project_dir="a/",
)
slug_root = allocate_deployed_slug("test-root")
create_deployed_app(
    slug=slug_root, name="test-root",
    source_session_id="sess-2", source_project_id="proj-1",
    owner_user_id="u-1", app_dir=f"/tmp/{slug_root}",
    project_dir=None,
)
print(f"Inserted test rows: {slug_a} (a/), {slug_root} (root)")

# A: same (sess, project_dir) → row
r = get_deployed_app_for_subapp("sess-1", "a/")
check("A: (sess-1, a/) returns the row", r is not None and r["slug"] == slug_a)

# B: different project_dir → None
r = get_deployed_app_for_subapp("sess-1", "b/")
check("B: (sess-1, b/) returns None", r is None)

# C: None against a project_dir row → None
r = get_deployed_app_for_subapp("sess-1", None)
check("C: (sess-1, None) against a/ row returns None", r is None)

# D: different session → None
r = get_deployed_app_for_subapp("sess-2", "a/")
check("D: (sess-2, a/) returns None", r is None)

# E: None/'' against a session-root row → row
r = get_deployed_app_for_subapp("sess-2", None)
check("E1: (sess-2, None) returns the root row", r is not None and r["slug"] == slug_root)
r = get_deployed_app_for_subapp("sess-2", "")
check("E2: (sess-2, '') returns the root row", r is not None and r["slug"] == slug_root)

# F: '' against a project_dir row → None
r = get_deployed_app_for_subapp("sess-1", "")
check("F: (sess-1, '') against a/ row returns None", r is None)

# cleanup
delete_deployed_app(slug_a)
delete_deployed_app(slug_root)
print("Cleaned up test rows.")

if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nAll checks passed.")
sys.exit(0)
