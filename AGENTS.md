# AGENTS.md

## Goal
Build a local service that can start an agent in any project root and communicate with OpenWebUI over HTTP requests only (no browser automation). OpenWebUI is hosted in a local network and reachable by domain.

## Current MVP Scope
Implement only the minimal flow:
1. Authenticate in OpenWebUI.
2. Persist cookies and auth-related data locally.
3. Get available models and select one.
4. Create a new chat.
5. Keep the session reusable between restarts.

## Non-Goals (for this stage)
1. Web UI.
2. Multi-user support.
3. Cloud deployment.
4. Complex orchestration of multiple agents.

## Preferred Operator UI (current stage)
Provide a compact desktop window (e.g., `tkinter`) that looks like a code-editor extension sidebar:
1. Left panel for status/auth/model/chat controls.
2. Message input field.
3. Result/output area for assistant responses.
4. No browser UI in this stage.

## Runtime Contract
Service is started from the root of a target project and stores its local state inside that project.

Required local directory:
`./.agent-service/`

Recommended files:
1. `./.agent-service/config.yaml`
2. `./.agent-service/auth.json`
3. `./.agent-service/cookies.json`
4. `./.agent-service/chats.json`
5. `./.agent-service/service.log`

## Minimal Config
`config.yaml` should support:

```yaml
openwebui:
  base_url: "https://openwebui.local"
  verify_tls: false
  endpoints:
    signin: "/api/v1/auths/signin"
    session_check: "/api/v1/auths/"
    models: "/api/models"
    chat_list: "/api/v1/chats/"
    chat_create: "/api/v1/chats/new"
    chat_completion: "/api/chat/completions"
  credentials:
    username: ""
    password: ""

agent:
  default_model: ""
  project_chat_autobind: true

http:
  timeout_seconds: 30
  retries: 2
  user_agent: "Mozilla/5.0 (AgentService/0.1)"
```

Notes:
1. Endpoint paths must stay configurable because OpenWebUI versions can differ.
2. Credentials can be omitted if valid persisted auth already exists.

## Required Service API (MVP)
Expose minimal internal API (HTTP or CLI wrappers over same service layer):

1. `POST /auth/login`
Input: optional `username`, `password` (fallback to config).
Output: auth status + saved session metadata.

2. `GET /auth/status`
Output: whether current local session is valid in OpenWebUI.

3. `GET /models`
Output: list of available models from OpenWebUI.

4. `POST /chats`
Input: `model_id`, optional `title`.
Output: created chat metadata (`chat_id`, model, created_at).

5. `POST /messages`
Input: `message`, optional `model_id`, optional `chat_id`.
Output: assistant response + metadata.

6. `GET /chats`
Output: available chats to continue context.

## Browser-like HTTP Behavior Requirements
1. Use a persistent HTTP session with cookie jar.
2. Always send realistic browser headers (`User-Agent`, `Accept`, `Accept-Language`, `Origin`, `Referer` where needed).
3. Follow redirects.
4. Capture and persist `Set-Cookie` values.
5. If OpenWebUI requires CSRF token, extract and resend it.
6. Do not use Playwright/Selenium; requests only.

## Auth and Session Flow
1. On startup, load `auth.json` and `cookies.json` if present.
2. Check existing session via `session_check` endpoint.
3. If invalid, perform `signin` request with credentials.
4. Save fresh cookies and auth metadata atomically.
5. On `401/403`, retry once after forced re-login.

## Model Selection Flow
1. Fetch model list from OpenWebUI.
2. Validate requested `model_id`.
3. If not provided, use `agent.default_model`.
4. Return explicit error if model is unavailable.

## Chat Creation Flow
1. Ensure valid auth.
2. Resolve model.
3. Call `chat_create` endpoint.
4. Persist created chat metadata in `chats.json`.
5. If `project_chat_autobind=true`, bind project path -> latest chat id.

## Storage and Security Requirements
1. Keep all session artifacts local to project (`./.agent-service`).
2. Never log plaintext password, tokens, or raw cookie values.
3. Redact sensitive values in logs.
4. Write files atomically and restrict permissions when possible.

## Observability
Minimal structured logs per request:
1. timestamp
2. action (`login`, `session_check`, `list_models`, `create_chat`)
3. endpoint
4. status_code
5. duration_ms
6. error_code (if any)

## Acceptance Criteria for MVP
1. With only `base_url`, credentials, and model id configured, service can login and create a chat.
2. After restart, service reuses stored cookies/session when valid.
3. If session expired, service automatically re-authenticates and continues.
4. Model validation prevents creating chat with unknown model.
5. All auth artifacts are stored only in `./.agent-service`.

## Implementation Notes
1. Prefer a typed service layer with clear interfaces:
   - `AuthClient`
   - `ModelClient`
   - `ChatClient`
   - `SessionStore`
2. Keep OpenWebUI-specific endpoint logic isolated in one adapter.
3. Add integration tests with mocked OpenWebUI responses for:
   - login success/failure
   - expired session recovery
   - model list and validation
   - chat creation
