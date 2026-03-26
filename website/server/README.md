# Server

The dashboard backend lives in `src/api/` (the existing FastAPI server).
No separate server process is needed — the dashboard routes are mounted
as an APIRouter on the main webhook API app.

## Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /api/dashboard/sessions` | REST | List sessions |
| `GET /api/dashboard/sessions/{id}/messages` | REST | Messages for a session |
| `GET /api/dashboard/tool-usage` | REST | Tool call records |
| `GET /api/dashboard/stats` | REST | Dashboard statistics |
| `GET /api/dashboard/stream` | SSE | Live agent activity stream |

## Authentication

Set `DASHBOARD_SECRET` (or falls back to `WEBHOOK_API_SECRET`) as a Bearer token.
If neither is set, the dashboard API is open (suitable for local dev only).
