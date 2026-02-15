from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import AppConfig
from .session_store import SessionStore

LOGGER = logging.getLogger(__name__)


class OpenWebUIError(RuntimeError):
    pass


class AuthenticationError(OpenWebUIError):
    pass


class ModelNotFoundError(OpenWebUIError):
    pass


class RequestFailedError(OpenWebUIError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: Any | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body

    def __str__(self) -> str:
        base = super().__str__()
        details = _extract_error_detail(self.response_body)
        if self.status_code is not None:
            base = f"{base} (status: {self.status_code})"
        if details:
            base = f"{base}: {details}"
        return base


class OpenWebUIClient:
    def __init__(self, config: AppConfig, store: SessionStore):
        self._config = config
        self._store = store
        self._client: httpx.AsyncClient | None = None
        self._csrf_token: str | None = None
        self._bearer_token: str | None = None
        self._bearer_token_type: str = "Bearer"

    async def startup(self) -> None:
        headers = {
            "User-Agent": self._config.http.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self._config.openwebui.base_url,
            "Referer": f"{self._config.openwebui.base_url}/",
        }

        self._client = httpx.AsyncClient(
            base_url=self._config.openwebui.base_url,
            timeout=self._config.http.timeout_seconds,
            headers=headers,
            follow_redirects=True,
            verify=self._config.openwebui.verify_tls,
            cookies=self._store.load_cookies(),
            trust_env=self._config.http.use_env_proxy,
        )
        self._restore_auth_header()

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def session_check(self) -> tuple[bool, dict[str, Any] | None]:
        candidates = self._endpoint_candidates(
            self._config.openwebui.endpoints.session_check,
            [
                "/api/v1/auths/",
                "/api/v1/auths",
                "/api/auths/",
                "/api/auths",
                "/api/v1/users/me",
                "/api/users/me",
            ],
        )

        last_failure: httpx.Response | None = None
        for endpoint in candidates:
            response = await self._request(
                action="session_check",
                method="GET",
                endpoint=endpoint,
                allow_retry=False,
            )

            if response.status_code == 200:
                payload = self._safe_json(response)
                if self._looks_like_html_response(payload):
                    continue

                self._sync_auth_from_payload(
                    payload,
                    auth_endpoint=endpoint,
                    logged_in=False,
                )
                self._config.openwebui.endpoints.session_check = endpoint
                return True, payload

            if response.status_code in {401, 403}:
                self._config.openwebui.endpoints.session_check = endpoint
                return False, None

            if response.status_code in {404, 405}:
                last_failure = response
                continue

            raise RequestFailedError(
                "OpenWebUI session check failed",
                status_code=response.status_code,
                response_body=self._safe_json(response),
            )

        failure_status = last_failure.status_code if last_failure is not None else None
        raise RequestFailedError(
            (
                "OpenWebUI session_check endpoint was not found. "
                f"Check openwebui.endpoints.session_check in config (tried: {', '.join(candidates)})"
            ),
            status_code=failure_status,
            response_body=self._safe_json(last_failure) if last_failure is not None else None,
        )

    async def login(self, username: str, password: str) -> dict[str, Any]:
        if not username or not password:
            raise AuthenticationError("Username/password are required for login")

        signin_candidates = self._endpoint_candidates(
            self._config.openwebui.endpoints.signin,
            [
                "/api/v1/auths/ldap",
                "/api/auths/ldap",
                "/api/v1/auth/ldap",
                "/api/auth/ldap",
                "/api/v1/auths/signin",
                "/api/auths/signin",
                "/api/v1/auth/signin",
                "/api/auth/signin",
            ],
        )

        last_response: httpx.Response | None = None
        for signin_endpoint in signin_candidates:
            endpoint_lower = signin_endpoint.lower()
            if "ldap" in endpoint_lower:
                payloads = [
                    {"user": username, "password": password},
                    {"username": username, "password": password},
                    {"email": username, "password": password},
                ]
            else:
                payloads = [
                    {"email": username, "password": password},
                    {"username": username, "password": password},
                    {"user": username, "password": password},
                ]

            endpoint_not_found = False
            for payload in payloads:
                response = await self._request(
                    action="login",
                    method="POST",
                    endpoint=signin_endpoint,
                    json=payload,
                    allow_retry=False,
                )

                if response.status_code == 200:
                    self._config.openwebui.endpoints.signin = signin_endpoint
                    payload_data = self._safe_json(response)
                    self._sync_auth_from_payload(
                        payload_data,
                        username=username,
                        auth_endpoint=signin_endpoint,
                        logged_in=True,
                    )
                    self._store.save_cookies(self._require_client().cookies)
                    return payload_data

                if response.status_code in {400, 401, 403, 422}:
                    last_response = response
                    continue

                if response.status_code in {404, 405}:
                    endpoint_not_found = True
                    last_response = response
                    break

                raise RequestFailedError(
                    "OpenWebUI login failed",
                    status_code=response.status_code,
                    response_body=self._safe_json(response),
                )

            if endpoint_not_found:
                continue

        status = last_response.status_code if last_response is not None else None
        if status in {404, 405}:
            raise RequestFailedError(
                (
                    "OpenWebUI signin endpoint was not found. "
                    f"Check openwebui.endpoints.signin in config (tried: {', '.join(signin_candidates)})"
                ),
                status_code=status,
                response_body=self._safe_json(last_response) if last_response is not None else None,
            )
        raise AuthenticationError(f"OpenWebUI login rejected credentials (status: {status})")

    async def list_models(self) -> list[dict[str, Any]]:
        response = await self._request(
            action="list_models",
            method="GET",
            endpoint=self._config.openwebui.endpoints.models,
        )

        if response.status_code in {401, 403}:
            raise AuthenticationError("OpenWebUI rejected session while fetching models")
        if response.status_code >= 400:
            raise RequestFailedError(
                "Failed to fetch models from OpenWebUI",
                status_code=response.status_code,
                response_body=self._safe_json(response),
            )

        return self._normalize_models(self._safe_json(response))

    async def list_chats(self) -> list[dict[str, Any]]:
        candidates = self._endpoint_candidates(
            self._config.openwebui.endpoints.chat_list,
            [
                "/api/v1/chats/",
                "/api/v1/chats",
                "/api/chats/",
                "/api/chats",
            ],
        )

        last_failure: httpx.Response | None = None
        for endpoint in candidates:
            response = await self._request(
                action="list_chats",
                method="GET",
                endpoint=endpoint,
            )

            if response.status_code == 200:
                payload = self._safe_json(response)
                self._config.openwebui.endpoints.chat_list = endpoint
                return self._normalize_chats(payload)

            if response.status_code in {401, 403}:
                raise AuthenticationError("OpenWebUI rejected session while fetching chats")

            if response.status_code in {404, 405}:
                last_failure = response
                continue

            raise RequestFailedError(
                "Failed to fetch chats from OpenWebUI",
                status_code=response.status_code,
                response_body=self._safe_json(response),
            )

        failure_status = last_failure.status_code if last_failure is not None else None
        raise RequestFailedError(
            (
                "OpenWebUI chat_list endpoint was not found. "
                f"Check openwebui.endpoints.chat_list in config (tried: {', '.join(candidates)})"
            ),
            status_code=failure_status,
            response_body=self._safe_json(last_failure) if last_failure is not None else None,
        )

    async def get_chat_history(self, chat_id: str) -> list[dict[str, str]]:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")

        candidates = self._chat_detail_candidates(clean_chat_id)
        last_failure: httpx.Response | None = None
        for endpoint in candidates:
            response = await self._request(
                action="get_chat_history",
                method="GET",
                endpoint=endpoint,
            )

            if response.status_code == 200:
                payload = self._safe_json(response)
                return self._extract_chat_messages(payload)

            if response.status_code in {401, 403}:
                raise AuthenticationError("OpenWebUI rejected session while fetching chat history")

            if response.status_code in {404, 405}:
                last_failure = response
                continue

            raise RequestFailedError(
                "Failed to fetch chat history",
                status_code=response.status_code,
                response_body=self._safe_json(response),
            )

        failure_status = last_failure.status_code if last_failure is not None else None
        raise RequestFailedError(
            (
                "OpenWebUI chat detail endpoint was not found. "
                f"Cannot fetch history (tried: {', '.join(candidates)})"
            ),
            status_code=failure_status,
            response_body=self._safe_json(last_failure) if last_failure is not None else None,
        )

    async def create_chat(self, model_id: str, title: str | None = None) -> dict[str, Any]:
        default_title = title or f"Agent chat {_utc_now_iso()}"
        payload_variants = [
            {"model": model_id, "title": default_title},
            {"chat": {"model": model_id, "title": default_title}},
        ]

        last_response: httpx.Response | None = None
        for payload in payload_variants:
            response = await self._request(
                action="create_chat",
                method="POST",
                endpoint=self._config.openwebui.endpoints.chat_create,
                json=payload,
            )

            if response.status_code in {200, 201}:
                return self._safe_json(response)
            if response.status_code in {401, 403}:
                raise AuthenticationError("OpenWebUI rejected session while creating chat")
            if response.status_code in {400, 404, 422}:
                last_response = response
                continue

            raise RequestFailedError(
                "Chat creation failed",
                status_code=response.status_code,
                response_body=self._safe_json(response),
            )

        if last_response is None:
            raise RequestFailedError("Chat creation failed without response body")

        raise RequestFailedError(
            "Chat creation payload rejected by OpenWebUI",
            status_code=last_response.status_code,
            response_body=self._safe_json(last_response),
        )

    async def update_chat_title(self, chat_id: str, title: str) -> dict[str, Any]:
        clean_chat_id = (chat_id or "").strip()
        clean_title = (title or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        if not clean_title:
            raise ValueError("title is required")

        candidates = self._chat_update_candidates(clean_chat_id)
        payloads = [
            {"title": clean_title},
            {"chat": {"title": clean_title}},
            {"chat_id": clean_chat_id, "title": clean_title},
            {"id": clean_chat_id, "title": clean_title},
        ]
        methods = ("PATCH", "PUT", "POST")

        last_failure: httpx.Response | None = None
        for endpoint in candidates:
            for method in methods:
                for payload in payloads:
                    response = await self._request(
                        action="rename_chat",
                        method=method,
                        endpoint=endpoint,
                        json=payload,
                    )

                    if response.status_code in {200, 201, 202, 204}:
                        if response.status_code == 204:
                            return {"chat_id": clean_chat_id, "title": clean_title, "updated": True}
                        body = self._safe_json(response)
                        if not body:
                            body = {}
                        body.setdefault("chat_id", clean_chat_id)
                        body.setdefault("title", clean_title)
                        body.setdefault("updated", True)
                        return body

                    if response.status_code in {401, 403}:
                        raise AuthenticationError("OpenWebUI rejected session while renaming chat")

                    if response.status_code in {400, 404, 405, 422}:
                        last_failure = response
                        continue

                    raise RequestFailedError(
                        "Chat rename failed",
                        status_code=response.status_code,
                        response_body=self._safe_json(response),
                    )

        failure_status = last_failure.status_code if last_failure is not None else None
        raise RequestFailedError(
            (
                "OpenWebUI chat rename endpoint was not found or payload was rejected. "
                f"Tried: {', '.join(candidates)}"
            ),
            status_code=failure_status,
            response_body=self._safe_json(last_failure) if last_failure is not None else None,
        )

    async def delete_chat(self, chat_id: str) -> dict[str, Any]:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")

        candidates = self._chat_delete_candidates(clean_chat_id)
        last_failure: httpx.Response | None = None
        for endpoint in candidates:
            response = await self._request(
                action="delete_chat",
                method="DELETE",
                endpoint=endpoint,
            )

            if response.status_code in {200, 202, 204}:
                if response.status_code == 204:
                    return {"chat_id": clean_chat_id, "deleted": True}
                payload = self._safe_json(response)
                if not payload:
                    payload = {}
                payload.setdefault("chat_id", clean_chat_id)
                payload.setdefault("deleted", True)
                return payload

            if response.status_code in {401, 403}:
                raise AuthenticationError("OpenWebUI rejected session while deleting chat")

            if response.status_code in {404, 405}:
                last_failure = response
                continue

            raise RequestFailedError(
                "Chat deletion failed",
                status_code=response.status_code,
                response_body=self._safe_json(response),
            )

        failure_status = last_failure.status_code if last_failure is not None else None
        raise RequestFailedError(
            (
                "OpenWebUI chat_delete endpoint was not found. "
                f"Check openwebui.endpoints.chat_delete in config (tried: {', '.join(candidates)})"
            ),
            status_code=failure_status,
            response_body=self._safe_json(last_failure) if last_failure is not None else None,
        )

    async def chat_completion(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        chat_id: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not model_id:
            raise ValueError("model_id is required")
        if not messages:
            raise ValueError("messages must not be empty")

        payload_base: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": False,
        }
        payload_templates: list[dict[str, Any]] = [dict(payload_base)]
        if tools:
            payload_templates = []
            payload_templates.append({**payload_base, "tools": tools, "tool_choice": "auto"})
            payload_templates.append({**payload_base, "tools": tools})
            functions = self._tools_to_functions(tools)
            if functions:
                payload_templates.append(
                    {
                        **payload_base,
                        "functions": functions,
                        "function_call": "auto",
                    }
                )
                payload_templates.append({**payload_base, "functions": functions})

        payload_variants: list[dict[str, Any]] = []
        for template in payload_templates:
            if chat_id:
                with_chat = dict(template)
                with_chat["chat_id"] = chat_id
                payload_variants.append(with_chat)
            payload_variants.append(dict(template))

        last_response: httpx.Response | None = None
        for payload in payload_variants:
            response = await self._request(
                action="chat_completion",
                method="POST",
                endpoint=self._config.openwebui.endpoints.chat_completion,
                json=payload,
            )
            if response.status_code == 200:
                return self._safe_json(response)
            if response.status_code in {401, 403}:
                raise AuthenticationError("OpenWebUI rejected session while running chat completion")
            if response.status_code in {400, 404, 422}:
                last_response = response
                continue

            raise RequestFailedError(
                "Chat completion failed",
                status_code=response.status_code,
                response_body=self._safe_json(response),
            )

        if last_response is None:
            raise RequestFailedError("Chat completion failed without response body")

        raise RequestFailedError(
            "OpenWebUI rejected chat completion payload",
            status_code=last_response.status_code,
            response_body=self._safe_json(last_response),
        )

    async def send_message(
        self,
        *,
        model_id: str,
        message: str,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        base_messages = [{"role": "user", "content": message}]
        payload_variants: list[dict[str, Any]] = [
            {
                "model": model_id,
                "messages": base_messages,
                "stream": False,
            },
            {
                "model": model_id,
                "messages": base_messages,
                "stream": False,
                "chat_id": chat_id,
            },
            {
                "model": model_id,
                "prompt": message,
                "stream": False,
            },
        ]
        if chat_id:
            payload_variants.insert(
                2,
                {
                    "chat_id": chat_id,
                    "model": model_id,
                    "messages": base_messages,
                    "stream": False,
                },
            )

        last_response: httpx.Response | None = None
        for payload in payload_variants:
            if payload.get("chat_id") is None:
                payload = {k: v for k, v in payload.items() if v is not None}

            response = await self._request(
                action="send_message",
                method="POST",
                endpoint=self._config.openwebui.endpoints.chat_completion,
                json=payload,
            )

            if response.status_code == 200:
                return self._safe_json(response)
            if response.status_code in {401, 403}:
                raise AuthenticationError("OpenWebUI rejected session while sending a message")
            if response.status_code in {400, 404, 422}:
                last_response = response
                continue

            raise RequestFailedError(
                "Message request failed",
                status_code=response.status_code,
                response_body=self._safe_json(response),
            )

        if last_response is None:
            raise RequestFailedError("Message request failed without response body")

        raise RequestFailedError(
            "OpenWebUI rejected message payload",
            status_code=last_response.status_code,
            response_body=self._safe_json(last_response),
        )

    async def _request(
        self,
        *,
        action: str,
        method: str,
        endpoint: str,
        allow_retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        client = self._require_client()

        attempts = self._config.http.retries + 1 if allow_retry else 1
        base_headers = dict(kwargs.pop("headers", {}))
        for attempt in range(attempts):
            started_at = time.perf_counter()
            try:
                headers = dict(base_headers)
                if self._csrf_token and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                    headers.setdefault("X-CSRF-Token", self._csrf_token)
                if self._bearer_token:
                    headers.setdefault(
                        "Authorization",
                        f"{self._bearer_token_type} {self._bearer_token}",
                    )

                response = await client.request(method=method, url=endpoint, headers=headers, **kwargs)
                self._update_csrf_token(response)

                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self._log_request(action, endpoint, response.status_code, duration_ms, error_code=None)

                if 500 <= response.status_code < 600 and attempt < attempts - 1:
                    continue

                return response
            except httpx.RequestError as exc:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self._log_request(
                    action,
                    endpoint,
                    status_code=None,
                    duration_ms=duration_ms,
                    error_code="request_error",
                )

                if attempt >= attempts - 1:
                    raise RequestFailedError(f"Network error talking to OpenWebUI: {exc}") from exc

        raise RequestFailedError("Unexpected request retry flow")

    @staticmethod
    def _normalize_models(payload: Any) -> list[dict[str, Any]]:
        candidates: list[Any]
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            for key in ("data", "models", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates = value
                    break
            else:
                candidates = [payload] if payload.get("id") else []
        else:
            candidates = []

        normalized: list[dict[str, Any]] = []
        for item in candidates:
            if isinstance(item, str):
                normalized.append({"id": item, "name": item, "raw": {"id": item}})
                continue

            if not isinstance(item, dict):
                continue

            model_id = item.get("id") or item.get("model") or item.get("name")
            if not model_id:
                continue

            normalized.append(
                {
                    "id": str(model_id),
                    "name": str(item.get("name") or model_id),
                    "raw": item,
                }
            )

        return normalized

    @staticmethod
    def _normalize_chats(payload: Any) -> list[dict[str, Any]]:
        candidates: list[Any]
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            for key in ("data", "items", "chats"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates = value
                    break
            else:
                candidates = [payload] if payload.get("id") else []
        else:
            candidates = []

        normalized: list[dict[str, Any]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue

            chat_id = item.get("chat_id") or item.get("chatId") or item.get("id")
            if not chat_id:
                continue

            title = item.get("title") or item.get("name") or ""
            model_id = item.get("model_id") or item.get("model")
            updated_at = (
                item.get("updated_at")
                or item.get("updatedAt")
                or item.get("created_at")
                or item.get("createdAt")
            )

            normalized.append(
                {
                    "chat_id": str(chat_id),
                    "title": str(title) if title is not None else "",
                    "model_id": str(model_id) if model_id else None,
                    "updated_at": str(updated_at) if updated_at else None,
                    "raw": item,
                }
            )

        return normalized

    def _update_csrf_token(self, response: httpx.Response) -> None:
        token = response.headers.get("x-csrf-token") or response.headers.get("X-CSRF-Token")
        if token:
            self._csrf_token = token

    def _restore_auth_header(self) -> None:
        stored = self._store.load_auth()
        if not isinstance(stored, dict):
            return

        token = stored.get("token")
        token_type = stored.get("token_type") or "Bearer"
        if isinstance(token, str) and token.strip():
            self._apply_auth_header(token.strip(), str(token_type))

    def _sync_auth_from_payload(
        self,
        payload: dict[str, Any],
        *,
        username: str | None = None,
        auth_endpoint: str | None = None,
        logged_in: bool,
    ) -> None:
        token = payload.get("token")
        token_type = payload.get("token_type") or "Bearer"

        if isinstance(token, str) and token.strip():
            self._apply_auth_header(token.strip(), str(token_type))

        base = self._store.load_auth()
        if not isinstance(base, dict):
            base = {}

        allowed_fields = (
            "id",
            "email",
            "name",
            "role",
            "expires_at",
            "permissions",
            "token",
            "token_type",
        )
        for key in allowed_fields:
            if key in payload:
                base[key] = payload[key]

        if username:
            base["username"] = username
        if auth_endpoint:
            base["auth_endpoint"] = auth_endpoint
        if logged_in:
            base["logged_in_at"] = _utc_now_iso()

        self._store.save_auth(base)

    def _apply_auth_header(self, token: str, token_type: str = "Bearer") -> None:
        token_type_clean = token_type.strip() or "Bearer"
        self._bearer_token = token
        self._bearer_token_type = token_type_clean

        client = self._client
        if client is not None:
            client.headers["Authorization"] = f"{token_type_clean} {token}"

    @staticmethod
    def _endpoint_candidates(primary: str, fallbacks: list[str]) -> list[str]:
        ordered: list[str] = []
        for endpoint in [primary, *fallbacks]:
            normalized = endpoint if endpoint.startswith("/") else f"/{endpoint}"
            if normalized not in ordered:
                ordered.append(normalized)
        return ordered

    def _chat_delete_candidates(self, chat_id: str) -> list[str]:
        templates = [
            self._config.openwebui.endpoints.chat_delete,
            "/api/v1/chats/{chat_id}",
            "/api/chats/{chat_id}",
            "/api/v1/chats/{chat_id}/delete",
            "/api/chats/{chat_id}/delete",
        ]

        chat_list_base = self._config.openwebui.endpoints.chat_list.rstrip("/")
        if chat_list_base:
            templates.extend(
                [
                    f"{chat_list_base}/{{chat_id}}",
                    f"{chat_list_base}/{{chat_id}}/delete",
                ]
            )

        ordered: list[str] = []
        for template in templates:
            normalized_template = template if template.startswith("/") else f"/{template}"
            if "{chat_id}" in normalized_template:
                candidate = normalized_template.replace("{chat_id}", chat_id)
            elif normalized_template.rstrip("/").endswith(chat_id):
                candidate = normalized_template
            else:
                candidate = f"{normalized_template.rstrip('/')}/{chat_id}"

            if candidate not in ordered:
                ordered.append(candidate)

        return ordered

    def _chat_update_candidates(self, chat_id: str) -> list[str]:
        templates = [
            "/api/v1/chats/{chat_id}",
            "/api/chats/{chat_id}",
            "/api/v1/chats/{chat_id}/title",
            "/api/chats/{chat_id}/title",
            "/api/v1/chats/{chat_id}/update",
            "/api/chats/{chat_id}/update",
        ]

        chat_list_base = self._config.openwebui.endpoints.chat_list.rstrip("/")
        if chat_list_base:
            templates.extend(
                [
                    f"{chat_list_base}/{{chat_id}}",
                    f"{chat_list_base}/{{chat_id}}/title",
                    f"{chat_list_base}/{{chat_id}}/update",
                ]
            )

        ordered: list[str] = []
        for template in templates:
            normalized_template = template if template.startswith("/") else f"/{template}"
            candidate = normalized_template.replace("{chat_id}", chat_id)
            if candidate not in ordered:
                ordered.append(candidate)

        return ordered

    def _chat_detail_candidates(self, chat_id: str) -> list[str]:
        templates = [
            "/api/v1/chats/{chat_id}",
            "/api/chats/{chat_id}",
            "/api/v1/chats/{chat_id}/messages",
            "/api/chats/{chat_id}/messages",
        ]

        chat_list_base = self._config.openwebui.endpoints.chat_list.rstrip("/")
        if chat_list_base:
            templates.extend(
                [
                    f"{chat_list_base}/{{chat_id}}",
                    f"{chat_list_base}/{{chat_id}}/messages",
                ]
            )

        ordered: list[str] = []
        for template in templates:
            normalized_template = template if template.startswith("/") else f"/{template}"
            candidate = normalized_template.replace("{chat_id}", chat_id)
            if candidate not in ordered:
                ordered.append(candidate)

        return ordered

    @staticmethod
    def _tools_to_functions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        functions: list[dict[str, Any]] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function":
                continue
            function_block = item.get("function")
            if isinstance(function_block, dict):
                functions.append(dict(function_block))
        return functions

    @staticmethod
    def _extract_chat_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
        containers: list[Any] = [payload]
        for key in ("data", "chat", "item"):
            value = payload.get(key)
            if value is not None:
                containers.append(value)

        for container in containers:
            messages = OpenWebUIClient._extract_messages_from_container(container)
            if messages:
                return messages

        return []

    @staticmethod
    def _extract_messages_from_container(container: Any) -> list[dict[str, str]]:
        if isinstance(container, list):
            parsed = OpenWebUIClient._parse_message_list(container)
            if parsed:
                return parsed

        if not isinstance(container, dict):
            return []

        direct_messages = container.get("messages")
        parsed_direct = OpenWebUIClient._parse_message_collection(
            direct_messages,
            current_id=container.get("currentId") or container.get("current_id"),
        )
        if parsed_direct:
            return parsed_direct

        history = container.get("history")
        if isinstance(history, dict):
            history_messages = history.get("messages")
            parsed_history = OpenWebUIClient._parse_message_collection(
                history_messages,
                current_id=history.get("currentId") or history.get("current_id"),
            )
            if parsed_history:
                return parsed_history

        for key in ("items", "data", "chat"):
            nested = container.get(key)
            nested_parsed = OpenWebUIClient._extract_messages_from_container(nested)
            if nested_parsed:
                return nested_parsed

        return []

    @staticmethod
    def _parse_message_collection(messages: Any, *, current_id: Any) -> list[dict[str, str]]:
        if isinstance(messages, list):
            return OpenWebUIClient._parse_message_list(messages)
        if isinstance(messages, dict):
            return OpenWebUIClient._parse_message_map(messages, current_id=current_id)
        return []

    @staticmethod
    def _parse_message_list(items: list[Any]) -> list[dict[str, str]]:
        parsed: list[dict[str, str]] = []
        for item in items:
            normalized = OpenWebUIClient._normalize_single_message(item)
            if normalized is not None:
                parsed.append(normalized)
        return parsed

    @staticmethod
    def _parse_message_map(
        items: dict[str, Any],
        *,
        current_id: Any,
    ) -> list[dict[str, str]]:
        parsed: list[dict[str, str]] = []

        current_id_text = str(current_id) if current_id is not None else ""
        if current_id_text and current_id_text in items:
            chain: list[str] = []
            seen: set[str] = set()
            node_id = current_id_text
            while node_id and node_id not in seen:
                seen.add(node_id)
                chain.append(node_id)
                raw_node = items.get(node_id)
                if not isinstance(raw_node, dict):
                    break
                parent_id = raw_node.get("parentId") or raw_node.get("parent_id")
                node_id = str(parent_id) if parent_id else ""
            chain.reverse()

            for key in chain:
                normalized = OpenWebUIClient._normalize_single_message(items.get(key))
                if normalized is not None:
                    parsed.append(normalized)
            if parsed:
                return parsed

        for value in items.values():
            normalized = OpenWebUIClient._normalize_single_message(value)
            if normalized is not None:
                parsed.append(normalized)
        return parsed

    @staticmethod
    def _normalize_single_message(item: Any) -> dict[str, str] | None:
        if not isinstance(item, dict):
            return None

        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            return None

        text = _normalize_content(item.get("content") or item.get("message") or item.get("text"))
        if not text:
            return None

        return {"role": role, "content": text}

    @staticmethod
    def _looks_like_html_response(payload: dict[str, Any]) -> bool:
        raw_text = payload.get("raw_text")
        if not isinstance(raw_text, str):
            return False
        lowered = raw_text.lower()
        return "<html" in lowered or "<!doctype html" in lowered

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            if isinstance(payload, list):
                return {"items": payload}
            return {"value": payload}
        except ValueError:
            return {"raw_text": response.text[:500]}

    @staticmethod
    def _log_request(
        action: str,
        endpoint: str,
        status_code: int | None,
        duration_ms: int,
        error_code: str | None,
    ) -> None:
        payload = {
            "timestamp": _utc_now_iso(),
            "action": action,
            "endpoint": endpoint,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "error_code": error_code,
        }
        LOGGER.info(json.dumps(payload, ensure_ascii=True))

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("OpenWebUI client is not started")
        return self._client


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_content(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item.strip())
                continue

            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())

        if parts:
            return "\n".join(parts)

    if isinstance(value, dict):
        text = value.get("content") or value.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()

    return None


def _extract_error_detail(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("detail", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if "raw_text" in payload:
            raw = payload.get("raw_text")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None
