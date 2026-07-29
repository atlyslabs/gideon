"""
Workflow-run lifecycle policy constants, shared by the execution service, the
scheduler's reaper, and the manual-run endpoint's concurrent-run guard.

This module MUST stay a leaf (import nothing from services/tools/routers).
routers/workflows_router.py needs these values for its concurrent-run guard,
and importing them from their historical homes triggers the circular import
chain: routers.workflows_router -> services.scheduler ->
services.workflow_execution_service -> tools.tool_executor ->
tools.tool_registry -> tools.workflow_tool -> routers.workflows_router.

The full rationale for each value lives next to its primary consumer:
- MAX_WALL_CLOCK_SECONDS: services/workflow_execution_service.py (the run
  loop's wall-clock cap; anything past it is a stuck tool-loop).
- STUCK_HEARTBEAT_SECONDS: services/scheduler.py (a `running` run whose
  heartbeat is older than this has no live owning process).
"""

# Soft wall-clock cap on a single workflow run (90 minutes; raised from 60
# with the round-cap bump so genuinely long, high-volume workflows can finish;
# still bounds a stuck loop).
MAX_WALL_CLOCK_SECONDS = 5400

# A run heartbeats (`last_progress_at`) at the top of every round. If a run is
# `running` but its heartbeat is older than this, the owning process is gone
# (pod crash / OOM / SIGKILL). Sits above the longest plausible single round
# (900s LLM timeout + tool execution) and below the wall-clock cap.
STUCK_HEARTBEAT_SECONDS = 1800  # 30 min
