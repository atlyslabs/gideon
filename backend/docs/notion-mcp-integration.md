# Notion MCP Integration

## Overview

Gideon connects to Notion via **Notion MCP** (Model Context Protocol). This avoids creating a traditional Notion developer integration and instead uses Notion's MCP OAuth infrastructure.

## How It Works

### Dynamic Client Registration (RFC 7591)

Notion MCP has an **open registration endpoint** at `https://mcp.notion.com/register`. No approval needed — any app can register.

**One-time registration:**
```bash
curl -X POST "https://mcp.notion.com/register" \
  -H "Content-Type: application/json" \
  -d '{
    "client_name": "Gideon",
    "redirect_uris": ["http://localhost:8000/api/gideon/v1/integrations/notion/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none"
  }'
```

Response:
```json
{
  "client_id": "lorDgDgmqKpLHA9J",
  "redirect_uris": ["http://localhost:8000/api/gideon/v1/integrations/notion/callback"],
  "client_name": "Gideon",
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

**Important:** Each call to `/register` returns a **new** `client_id`. Store it and reuse — don't re-register.

### OAuth Flow (PKCE, no client_secret)

This is a **public client** (`token_endpoint_auth_method: "none"`), secured with PKCE (S256) instead of a client secret.

```
User clicks Connect
    → Redirect to mcp.notion.com/authorize
        (with client_id, redirect_uri, code_challenge, state)
    → User sees Notion consent screen ("Connect with Notion MCP")
    → User grants access
    → Notion redirects to our callback with ?code=...&state=...
    → Backend exchanges code + code_verifier at mcp.notion.com/token
    → Receives access_token + refresh_token
```

### Endpoints

| Endpoint | URL |
|----------|-----|
| Registration | `POST https://mcp.notion.com/register` |
| Authorization | `https://mcp.notion.com/authorize` |
| Token Exchange | `POST https://mcp.notion.com/token` |
| MCP Server | `https://mcp.notion.com/mcp` |
| Discovery | `https://mcp.notion.com/.well-known/oauth-authorization-server` |

### Token Lifecycle

- **Access tokens expire in 1 hour**
- **Refresh tokens rotate** — new refresh token returned on each refresh
- Token exchange uses `application/x-www-form-urlencoded` (not JSON)
- No client_secret needed — PKCE code_verifier is the proof

### Token Exchange Request

```
POST https://mcp.notion.com/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code&
code=<auth_code>&
client_id=lorDgDgmqKpLHA9J&
redirect_uri=http://localhost:8000/api/gideon/v1/integrations/notion/callback&
code_verifier=<code_verifier>
```

### Token Refresh Request

```
POST https://mcp.notion.com/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&
refresh_token=<refresh_token>&
client_id=lorDgDgmqKpLHA9J
```

## Key Differences from Regular Notion OAuth

| | Regular Notion OAuth | Notion MCP OAuth |
|---|---|---|
| Registration | Create integration at notion.so/my-integrations | POST to mcp.notion.com/register |
| Auth method | client_secret (Basic auth) | PKCE (no secret) |
| Token expiry | Never expires | 1 hour (refresh needed) |
| Consent screen | "Add to workspace" | "Connect with Notion MCP" |
| Credentials needed | client_id + client_secret | client_id only |
| Capabilities | Standard API | Standard API + MCP features |

## Current Gideon Config

- **Client ID:** `lorDgDgmqKpLHA9J`
- **Redirect URI:** `http://localhost:8000/api/gideon/v1/integrations/notion/callback`
- **Code:** `services/gideon/services/oauth/notion_oauth.py`

## Enterprise Considerations

Enterprise Notion workspaces can restrict MCP clients via admin settings (Settings > Notion AI > AI connectors). If a user's workspace blocks unrecognized clients, they won't be able to connect.

## References

- [Notion MCP: Get Started](https://developers.notion.com/guides/mcp/get-started-with-mcp)
- [Notion MCP: Build a Custom Client](https://developers.notion.com/guides/mcp/build-mcp-client)
- [MCP Spec: Authorization](https://modelcontextprotocol.io/specification/2025-03-26/basic/authorization)
- [Notion Help: MCP](https://www.notion.com/help/notion-mcp)
