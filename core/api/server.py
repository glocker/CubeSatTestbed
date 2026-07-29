"""
Backend API (FastAPI) serving:
- node/config CRUD (create/edit/reload a test setup)
- fault injection endpoints
- scenario run trigger + live results
- a live telemetry feed (WebSocket) for the frontend and the optional
  OpenMCT bridge

Planned: auth is out of scope for v1 -- this is a local/lab tool, not a
hardened multi-tenant service.
"""
