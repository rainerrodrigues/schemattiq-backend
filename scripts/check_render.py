"""Read-only check of the Render workspace: lists workflows and their registered
tasks, so a "create task failed: not found" can be traced to its cause.

    python scripts/check_render.py

Uses RENDER_API_KEY from the environment or .env. Runs no tasks.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import greptile_hw  # noqa: E402,F401 - loads .env into os.environ

API = "https://api.render.com/v1"
EXPECTED = os.getenv("PIPELINE_TASK_SLUG", "schemattiq-workflow/schemattiq_pipeline")


def get(path: str) -> list:
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {os.environ['RENDER_API_KEY']}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"GET {path} -> {e.code}: {e.read().decode(errors='replace')[:300]}")


def unwrap(rows: list, key: str) -> list:
    # Render list endpoints return [{"cursor": ..., "<key>": {...}}, ...]
    return [r.get(key, r) for r in rows]


def main() -> int:
    if not os.getenv("RENDER_API_KEY"):
        sys.exit("Set RENDER_API_KEY in .env first")
    workflows = unwrap(get("/workflows?limit=100"), "workflow")
    print(f"Workflows visible to this API key: {len(workflows)}")
    for w in workflows:
        print(f"  {w.get('name')!r}  slug={w.get('slug')!r}  id={w.get('id')}  owner={w.get('ownerId')}")

    # Task names are listed without the workflow prefix; a run is addressed as
    # "<workflow slug>/<task name>".
    wf_slug = {w.get("id"): w.get("slug") for w in workflows}
    tasks = unwrap(get("/tasks?limit=100"), "task")
    print(f"\nRegistered tasks: {len(tasks)}")
    slugs = set()
    for t in tasks:
        slug = f"{wf_slug.get(t.get('workflowId'), '?')}/{t.get('name')}"
        slugs.add(slug)
        print(f"  {slug}  (version {t.get('workflowVersionId')})")

    print(f"\nPIPELINE_TASK_SLUG = {EXPECTED!r}")
    if EXPECTED in slugs:
        print("OK: the pipeline task is registered under that name.")
        return 0
    if not workflows:
        print("No workflows: this API key is from a different workspace, or the Workflow wasn't created.")
    elif not tasks:
        print("Workflow exists but no tasks registered: its deploy failed or is still building. Check its logs.")
    else:
        print("Task name differs: set PIPELINE_TASK_SLUG on the web service to one of the names above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
