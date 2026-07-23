"""Action layer — tools that DO things in the real world (send email,
write files, post to Slack), not just fetch data.

Design mirrors backend/app/tools/http_tool_runner.py so the MCP planner
can union all three sources (MCP servers + custom HTTP tools + built-in
actions) behind one uniform tool listing. Actions live in their own
namespace `action.*` and route to `ActionRegistry.call()`.

Mutating actions never fire directly. They enqueue a PendingAction and
return a receipt string; the founder approves via the UI, and the
action runs against the environment.
"""
