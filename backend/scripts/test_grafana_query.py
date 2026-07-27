"""
Quick test script for Grafana panel queries.
Tests variable substitution + query execution without needing the full app.

Usage:
  poetry run python scripts/test_grafana_query.py
"""

import asyncio
import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from config import GRAFANA_URL, GRAFANA_TOKEN
from tools.grafana_tool import _substitute_variables, _ALL_VARS_SENTINEL

DASHBOARD_UID = "e67d985c-444b-4494-be61-508f2d9804be"
PANEL_ID = 188
FROM_TIME = "now-12M"
TO_TIME = "now"

# User overrides (what Claude would pass)
USER_OVERRIDES = {
    "date_aggregation": "week",
    "geoip_country_code": "IN",
    "destination_country": "VN",
    "source": "organic",
}


def _headers():
    return {"Authorization": f"Bearer {GRAFANA_TOKEN}", "Accept": "application/json"}


async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Fetch dashboard
        print("Fetching dashboard...")
        resp = await client.get(f"{GRAFANA_URL}/api/dashboards/uid/{DASHBOARD_UID}", headers=_headers())
        assert resp.status_code == 200, f"Dashboard fetch failed: {resp.status_code}"
        dashboard = resp.json().get("dashboard", {})

        # 2. Build variables from dashboard defaults
        all_variables = {}
        for v in dashboard.get("templating", {}).get("list", []):
            current = v.get("current", {})
            val = current.get("value")
            if not val:
                continue
            if val == "$__all" or val == ["$__all"]:
                all_variables[v["name"]] = _ALL_VARS_SENTINEL
            elif isinstance(val, str):
                all_variables[v["name"]] = val
            elif isinstance(val, (list, tuple)):
                filtered = [str(x) for x in val if x != "$__all"]
                all_variables[v["name"]] = ",".join(filtered) if filtered else _ALL_VARS_SENTINEL
            else:
                all_variables[v["name"]] = str(val)

        # 3. Apply user overrides
        all_variables.update(USER_OVERRIDES)

        # 4. Auto-infer funnel steps from panel title
        panel = None
        for p in dashboard.get("panels", []):
            if p.get("id") == PANEL_ID:
                panel = p
                break
            for sub in p.get("panels", []):
                if sub.get("id") == PANEL_ID:
                    panel = sub
                    break
        assert panel, f"Panel {PANEL_ID} not found"

        panel_title = panel.get("title", "")
        clean_title = re.sub(r"\$\{?\w+\}?", "", panel_title).strip()
        arrow_match = re.search(r"(\S+)\s*(?:->|→)\s*(\S+)", clean_title)
        if arrow_match:
            step_left = arrow_match.group(1).strip()
            step_right = arrow_match.group(2).strip()
            templating = dashboard.get("templating", {}).get("list", [])
            for v in templating:
                vname = v.get("name", "")
                if "step" not in vname.lower() and "funnel" not in vname.lower():
                    continue
                opts = {o.get("value", "") for o in v.get("options", []) if o.get("value") and o.get("value") != "$__all"}
                if vname not in USER_OVERRIDES:
                    if any(k in vname.lower() for k in ("_1", "step_1", "entering", "from")):
                        if step_left in opts:
                            all_variables[vname] = step_left
                    elif any(k in vname.lower() for k in ("_2", "step_2", "converting", "to")):
                        if step_right in opts:
                            all_variables[vname] = step_right

        # 5. Print active filters
        active = {k: v for k, v in all_variables.items() if v != _ALL_VARS_SENTINEL}
        print(f"\n=== Active filters ===")
        for k, v in sorted(active.items()):
            print(f"  {k}: {v}")

        all_vars_that_are_all = [k for k, v in all_variables.items() if v == _ALL_VARS_SENTINEL]
        print(f"\n=== $__all variables ({len(all_vars_that_are_all)}) ===")
        print(f"  {', '.join(sorted(all_vars_that_are_all))}")

        # 6. Build query with variable substitution
        targets = panel.get("targets", [])
        datasource = panel.get("datasource", {})
        ds_uid = datasource.get("uid") if isinstance(datasource, dict) else None

        queries = []
        for i, target in enumerate(targets):
            q = {**target}
            if ds_uid:
                q["datasource"] = {"uid": ds_uid}
            if "refId" not in q:
                q["refId"] = chr(65 + i)

            raw_before = q.get("rawSql", "")
            if all_variables:
                q = _substitute_variables(q, all_variables)
            raw_after = q.get("rawSql", "")

            print(f"\n=== SQL After Substitution (Target {q.get('refId', '?')}, {len(raw_after)} chars) ===")
            print(raw_after[:3000])
            if len(raw_after) > 3000:
                print(f"... (truncated, {len(raw_after)} total)")

            # Check for remaining unsubstituted variables
            remaining = re.findall(r"\$\{?\w+(?::\w+)?\}?", raw_after)
            grafana_macros = [r for r in remaining if r.startswith("$__")]
            user_vars = [r for r in remaining if not r.startswith("$__")]
            if user_vars:
                print(f"\n  WARNING: Unsubstituted variables: {user_vars}")
            if grafana_macros:
                print(f"\n  Grafana macros (handled server-side): {grafana_macros}")

            queries.append(q)

        # 7. Execute query
        payload = {"queries": queries, "from": FROM_TIME, "to": TO_TIME}
        print(f"\n=== Executing query... ===")
        query_resp = await client.post(f"{GRAFANA_URL}/api/ds/query", json=payload, headers=_headers())

        if query_resp.status_code != 200:
            print(f"QUERY FAILED: {query_resp.status_code}")
            print(query_resp.text[:1000])
            return

        result = query_resp.json()
        frames = result.get("results", {})

        for ref_id, frame_data in frames.items():
            for frame in frame_data.get("frames", []):
                schema = frame.get("schema", {})
                data = frame.get("data", {})
                fields = [f.get("name", f"field_{i}") for i, f in enumerate(schema.get("fields", []))]
                values = data.get("values", [])
                num_rows = len(values[0]) if values else 0

                print(f"\n=== Results ({ref_id}): {num_rows} rows, fields: {fields} ===")
                # Print first 5 and last 5 rows
                for idx in range(min(5, num_rows)):
                    row = {fields[c]: values[c][idx] for c in range(len(fields)) if c < len(values)}
                    print(f"  [{idx}] {row}")
                if num_rows > 10:
                    print(f"  ... ({num_rows - 10} more rows)")
                    for idx in range(max(5, num_rows - 5), num_rows):
                        row = {fields[c]: values[c][idx] for c in range(len(fields)) if c < len(values)}
                        print(f"  [{idx}] {row}")


if __name__ == "__main__":
    asyncio.run(main())
