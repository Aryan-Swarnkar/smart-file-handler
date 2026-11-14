"""AccessService.resolve_role(actor, resource) -> owner | editor | viewer | None.

The ONLY authorization gate: every service method takes an explicit actor and
calls this first; no access raises NotFound (404, never 403). The role is the
highest of ownership, a direct grant, or a grant on any ancestor folder
(recursive CTE, depth cap 20); a trashed ancestor makes the subtree
inaccessible except to the owner via trash. Phase 4.
May depend on: repositories.folders, repositories.permissions.
"""
