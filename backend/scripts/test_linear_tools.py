"""End-to-end test of the new Linear project + issue-move tools.

Flow:
  1. Decrypt the Linear access token using Fernet (ENCRYPTION_KEY from env).
  2. List teams and let the caller pick one (or pass TEAM_NAME env).
  3. create_linear_project → "Gideon Tool Test <ts>"
  4. update_linear_project → patch description
  5. create_linear_issue  → create one issue directly inside the project
  6. update_linear_issue  → move that issue out (empty project_id) to detach
  7. update_linear_issue  → move it back into the project
  8. Print a cleanup URL (Linear projects API has no delete — user archives manually)

Run:
  cd services/gideon
  export $(grep -E '^(ENCRYPTION_KEY|DATABASE_URL)=' .env | xargs)
  export LINEAR_ENC_TOKEN='<paste the access_token ciphertext>'
  export TEAM_NAME='Gideon'   # optional; fallback prompts interactively
  poetry run python scripts/test_linear_tools.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

# Make the package importable when run as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet  # noqa: E402


def _decrypt(ciphertext: str, key: str) -> str:
    return Fernet(key.encode()).decrypt(ciphertext.encode()).decode()


async def main() -> None:
    enc_token = os.environ.get("LINEAR_ENC_TOKEN")
    enc_key = os.environ.get("ENCRYPTION_KEY")
    if not enc_token or not enc_key:
        print("Need LINEAR_ENC_TOKEN and ENCRYPTION_KEY in env.")
        sys.exit(1)

    access_token = _decrypt(enc_token, enc_key)
    print("✓ Decrypted access token (not shown)")

    # Import AFTER env is loaded so config / encryption modules pick it up.
    from tools.linear_tool import (
        ListLinearTeams,
        CreateLinearProject,
        UpdateLinearProject,
        CreateLinearIssue,
        UpdateLinearIssue,
    )

    # --- Pick a team ---
    teams_result = await ListLinearTeams().execute({}, access_token)
    teams = teams_result.get("teams", [])
    if not teams:
        print("No teams found. Aborting.")
        sys.exit(1)

    wanted = os.environ.get("TEAM_NAME", "").lower().strip()
    chosen = next((t for t in teams if wanted and wanted in t["name"].lower()), None) or teams[0]
    print(f"✓ Using team: {chosen['name']} ({chosen['id']})")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    project_name = f"Gideon Tool Test {ts}"

    # --- Create project ---
    print(f"\n[1/5] create_linear_project — {project_name!r}")
    created_project = await CreateLinearProject().execute(
        {
            "name": project_name,
            "description": "Initial description — will be patched.",
            "team_ids": [chosen["id"]],
        },
        access_token,
    )
    if "error" in created_project:
        print(f"  ✗ {created_project['error']}")
        sys.exit(1)
    project_id = created_project["id"]
    print(f"  ✓ created {project_id} — {created_project.get('url')}")

    # --- Update project description ---
    print("\n[2/5] update_linear_project — patch description")
    updated_project = await UpdateLinearProject().execute(
        {
            "project_id": project_id,
            "description": "## Patched via Gideon test\n\nThis description was set by `update_linear_project`.",
        },
        access_token,
    )
    if "error" in updated_project:
        print(f"  ✗ {updated_project['error']}")
        sys.exit(1)
    desc = (updated_project.get("description") or "").split("\n")[0]
    print(f"  ✓ description now begins: {desc!r}")

    # --- Create issue directly in the project ---
    print("\n[3/5] create_linear_issue with project_id")
    created_issue = await CreateLinearIssue().execute(
        {
            "title": f"Test issue (created inside project) — {ts}",
            "description": "Seeded by scripts/test_linear_tools.py.",
            "team_id": chosen["id"],
            "project_id": project_id,
        },
        access_token,
    )
    if "error" in created_issue:
        print(f"  ✗ {created_issue['error']}")
        sys.exit(1)
    issue_id = created_issue["id"]
    issue_ident = created_issue["identifier"]
    print(f"  ✓ created {issue_ident} ({issue_id}) — {created_issue.get('url')}")

    # --- Detach the issue (project_id="") ---
    print("\n[4/5] update_linear_issue — detach from project (project_id='')")
    detached = await UpdateLinearIssue().execute(
        {"issue_id": issue_id, "project_id": ""},
        access_token,
    )
    if "error" in detached:
        print(f"  ✗ {detached['error']}")
        sys.exit(1)
    print(f"  ✓ issue {issue_ident} detached from project")

    # --- Move the issue back into the project ---
    print("\n[5/5] update_linear_issue — move back into project")
    re_attached = await UpdateLinearIssue().execute(
        {"issue_id": issue_id, "project_id": project_id},
        access_token,
    )
    if "error" in re_attached:
        print(f"  ✗ {re_attached['error']}")
        sys.exit(1)
    print(f"  ✓ issue {issue_ident} moved back into {project_name!r}")

    print("\n----------------------------------------")
    print("All 5 Linear tool calls succeeded.")
    print(f"Cleanup: archive/delete project {project_id} and issue {issue_ident} in Linear UI.")
    print("Project URL:", created_project.get("url"))
    print("Issue URL:  ", created_issue.get("url"))


if __name__ == "__main__":
    asyncio.run(main())
