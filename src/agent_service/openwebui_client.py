from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

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
        payload_variants = self._build_completion_payload_variants(
            model_id=model_id,
            messages=messages,
            chat_id=chat_id,
            tools=tools,
            stream=False,
        )

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

    async def chat_completion_stream(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        chat_id: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not model_id:
            raise ValueError("model_id is required")
        if not messages:
            raise ValueError("messages must not be empty")

        payload_variants = self._build_completion_payload_variants(
            model_id=model_id,
            messages=messages,
            chat_id=chat_id,
            tools=tools,
            stream=True,
        )

        last_error: RequestFailedError | None = None
        for payload in payload_variants:
            try:
                return await self._request_chat_completion_stream(payload=payload, on_event=on_event)
            except RequestFailedError as exc:
                if exc.status_code in {400, 404, 422}:
                    last_error = exc
                    continue
                raise

        if last_error is not None:
            raise RequestFailedError(
                "OpenWebUI rejected chat completion payload",
                status_code=last_error.status_code,
                response_body=last_error.response_body,
            )
        raise RequestFailedError("Chat completion stream failed without response body")

    async def _request_chat_completion_stream(
        self,
        *,
        payload: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        client = self._require_client()
        endpoint = self._config.openwebui.endpoints.chat_completion
        attempts = self._config.http.retries + 1
        for attempt in range(attempts):
            started_at = time.perf_counter()
            try:
                async with client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    self._update_csrf_token(response)
                    status_code = response.status_code
                    if status_code in {401, 403}:
                        duration_ms = int((time.perf_counter() - started_at) * 1000)
                        self._log_request(
                            "chat_completion_stream",
                            endpoint,
                            status_code,
                            duration_ms,
                            error_code=None,
                        )
                        raise AuthenticationError(
                            "OpenWebUI rejected session while running chat completion stream"
                        )

                    if status_code >= 400:
                        body = await response.aread()
                        del body
                        payload_error = self._safe_json(response)
                        duration_ms = int((time.perf_counter() - started_at) * 1000)
                        self._log_request(
                            "chat_completion_stream",
                            endpoint,
                            status_code,
                            duration_ms,
                            error_code=None,
                        )
                        if 500 <= status_code < 600 and attempt < attempts - 1:
                            continue
                        raise RequestFailedError(
                            "Chat completion stream failed",
                            status_code=status_code,
                            response_body=payload_error,
                        )

                    result = await self._collect_chat_stream(response, on_event=on_event)
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    self._log_request(
                        "chat_completion_stream",
                        endpoint,
                        status_code,
                        duration_ms,
                        error_code=None,
                    )
                    return result
            except httpx.RequestError as exc:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self._log_request(
                    "chat_completion_stream",
                    endpoint,
                    status_code=None,
                    duration_ms=duration_ms,
                    error_code=self._request_error_code(exc),
                )
                if attempt >= attempts - 1:
                    details = self._request_error_message(exc, endpoint=endpoint)
                    raise RequestFailedError(f"Network error talking to OpenWebUI: {details}") from exc

        raise RequestFailedError("Unexpected stream retry flow")

    async def _collect_chat_stream(
        self,
        response: httpx.Response,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        assistant_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        finish_reason: str | None = None
        resolved_chat_id: str | None = None
        final_message_content: str | None = None
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        tool_call_order: list[int] = []
        saw_assistant_delta = False

        async for chunk in self._iter_sse_chunks(response):
            if not isinstance(chunk, dict):
                continue
            chunk_dict = chunk.get("data") if isinstance(chunk.get("data"), dict) else chunk
            if not isinstance(chunk_dict, dict):
                continue

            candidate_chat_id = (
                chunk_dict.get("chat_id")
                or chunk_dict.get("chatId")
                or (chunk_dict.get("chat", {}) or {}).get("id")
            )
            if candidate_chat_id:
                resolved_chat_id = str(candidate_chat_id).strip() or resolved_chat_id

            choices = chunk_dict.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0] if isinstance(choices[0], dict) else {}
                if not isinstance(first, dict):
                    first = {}

                finish_value = first.get("finish_reason")
                if isinstance(finish_value, str) and finish_value.strip():
                    finish_reason = finish_value.strip()

                delta = first.get("delta")
                if isinstance(delta, dict):
                    text_delta = self._extract_stream_text(delta.get("content") or delta.get("text"))
                    if text_delta:
                        saw_assistant_delta = True
                        assistant_chunks.append(text_delta)
                        self._notify_stream_event(on_event, {"type": "assistant_delta", "text": text_delta})

                    reasoning_delta = self._extract_stream_reasoning(delta)
                    if reasoning_delta:
                        reasoning_chunks.append(reasoning_delta)
                        self._notify_stream_event(on_event, {"type": "reasoning_delta", "text": reasoning_delta})

                    self._merge_stream_tool_calls(
                        tool_calls_by_index,
                        tool_call_order,
                        delta.get("tool_calls"),
                        on_event=on_event,
                    )
                    self._merge_stream_function_call(
                        tool_calls_by_index,
                        tool_call_order,
                        delta.get("function_call"),
                        on_event=on_event,
                    )

                message = first.get("message")
                if isinstance(message, dict):
                    full_content = self._extract_stream_text(message.get("content"))
                    if full_content and not saw_assistant_delta:
                        final_message_content = full_content
                    reasoning_full = self._extract_stream_reasoning(message)
                    if reasoning_full:
                        reasoning_chunks.append(reasoning_full)
                        self._notify_stream_event(on_event, {"type": "reasoning_delta", "text": reasoning_full})
                    self._merge_stream_tool_calls(
                        tool_calls_by_index,
                        tool_call_order,
                        message.get("tool_calls"),
                        on_event=on_event,
                    )
                    self._merge_stream_function_call(
                        tool_calls_by_index,
                        tool_call_order,
                        message.get("function_call"),
                        on_event=on_event,
                    )

                text_value = first.get("text")
                if isinstance(text_value, str) and text_value:
                    saw_assistant_delta = True
                    assistant_chunks.append(text_value)
                    self._notify_stream_event(on_event, {"type": "assistant_delta", "text": text_value})

            top_response = chunk_dict.get("response")
            if isinstance(top_response, str) and top_response:
                saw_assistant_delta = True
                assistant_chunks.append(top_response)
                self._notify_stream_event(on_event, {"type": "assistant_delta", "text": top_response})

            message_value = chunk_dict.get("message")
            done_flag = bool(chunk_dict.get("done"))
            if isinstance(message_value, dict):
                message_text = self._extract_stream_text(message_value.get("content"))
                if message_text:
                    if done_flag and not saw_assistant_delta:
                        final_message_content = message_text
                    elif not done_flag:
                        saw_assistant_delta = True
                        assistant_chunks.append(message_text)
                        self._notify_stream_event(on_event, {"type": "assistant_delta", "text": message_text})
                reasoning_msg = self._extract_stream_reasoning(message_value)
                if reasoning_msg:
                    reasoning_chunks.append(reasoning_msg)
                    self._notify_stream_event(on_event, {"type": "reasoning_delta", "text": reasoning_msg})
                self._merge_stream_tool_calls(
                    tool_calls_by_index,
                    tool_call_order,
                    message_value.get("tool_calls"),
                    on_event=on_event,
                )
                self._merge_stream_function_call(
                    tool_calls_by_index,
                    tool_call_order,
                    message_value.get("function_call"),
                    on_event=on_event,
                )

            if done_flag and isinstance(chunk_dict.get("done_reason"), str):
                done_reason = str(chunk_dict.get("done_reason") or "").strip()
                if done_reason:
                    finish_reason = done_reason

            if "choices" not in chunk_dict and "message" not in chunk_dict:
                top_reasoning = self._extract_stream_reasoning(chunk_dict)
                if top_reasoning:
                    reasoning_chunks.append(top_reasoning)
                    self._notify_stream_event(on_event, {"type": "reasoning_delta", "text": top_reasoning})

        assistant_text = "".join(assistant_chunks).strip() if assistant_chunks else (final_message_content or "")
        reasoning_text = "".join(reasoning_chunks).strip()
        tool_calls = self._finalize_stream_tool_calls(tool_calls_by_index, tool_call_order)

        message: dict[str, Any] = {
            "role": "assistant",
            "content": assistant_text,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        if reasoning_text:
            message["reasoning"] = reasoning_text

        result: dict[str, Any] = {
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ]
        }
        if resolved_chat_id:
            result["chat_id"] = resolved_chat_id
        return result

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
                    error_code=self._request_error_code(exc),
                )

                if attempt >= attempts - 1:
                    details = self._request_error_message(exc, endpoint=endpoint)
                    raise RequestFailedError(f"Network error talking to OpenWebUI: {details}") from exc

        raise RequestFailedError("Unexpected request retry flow")

    def _request_error_message(self, exc: httpx.RequestError, *, endpoint: str) -> str:
        error_type = exc.__class__.__name__
        detail = str(exc).strip()

        target = ""
        request = getattr(exc, "request", None)
        if request is not None:
            try:
                target = str(request.url)
            except Exception:  # noqa: BLE001
                target = ""
        if not target:
            target = f"{self._config.openwebui.base_url}{endpoint}"

        if isinstance(exc, httpx.TimeoutException):
            timeout_seconds = int(self._config.http.timeout_seconds)
            message = f"{error_type} after {timeout_seconds}s while requesting {target}"
        else:
            message = f"{error_type} while requesting {target}"

        if detail and detail not in {error_type, message}:
            message = f"{message}: {detail}"
        return message

    @staticmethod
    def _request_error_code(exc: httpx.RequestError) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "request_timeout"
        if isinstance(exc, httpx.ConnectError):
            return "connect_error"
        if isinstance(exc, httpx.NetworkError):
            return "network_error"
        return "request_error"

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

    def _build_completion_payload_variants(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        chat_id: str | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> list[dict[str, Any]]:
        payload_base: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": stream,
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
        return payload_variants

    @staticmethod
    def _notify_stream_event(
        callback: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any],
    ) -> None:
        if callback is None:
            return
        try:
            callback(event)
        except Exception:  # noqa: BLE001
            LOGGER.debug("stream callback failed", exc_info=True)

    @staticmethod
    async def _iter_sse_chunks(response: httpx.Response) -> AsyncIterator[Any]:
        data_lines: list[str] = []

        async def flush_data() -> AsyncIterator[Any]:
            if not data_lines:
                return
            payload = "\n".join(data_lines).strip()
            data_lines.clear()
            if not payload:
                return
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                return

        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\n")
            if line == "":
                async for item in flush_data():
                    yield item
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith(":"):
                continue
            if stripped.startswith("event:"):
                continue
            if stripped.startswith("id:"):
                continue
            if stripped.startswith("retry:"):
                continue
            if stripped.startswith("data:"):
                data_lines.append(stripped[5:].lstrip())
                continue

            # Fallback for non-SSE newline-delimited JSON.
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError:
                continue

        async for item in flush_data():
            yield item
    @staticmethod
    def _extract_stream_text(value: Any) -> str:
        reasoning_types = {"reasoning", "thinking", "analysis", "thought"}
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            value_type = str(value.get("type") or "").strip().lower()
            if value_type in reasoning_types:
                return ""
            nested = value.get("text")
            if isinstance(nested, str):
                return nested
            nested = value.get("content")
            if isinstance(nested, str):
                return nested
            return ""
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                text = OpenWebUIClient._extract_stream_text(item)
                if text:
                    parts.append(text)
            return "".join(parts)
        return ""

    @staticmethod
    def _extract_stream_reasoning(value: Any) -> str:
        reasoning_keys = (
            "reasoning",
            "reasoning_content",
            "thinking",
            "analysis",
            "thought",
            "reasoning_text",
        )
        reasoning_types = {"reasoning", "thinking", "analysis", "thought"}

        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                text = OpenWebUIClient._extract_stream_reasoning(item)
                if text:
                    parts.append(text)
            return "".join(parts)
        if not isinstance(value, dict):
            return ""

        parts: list[str] = []
        value_type = str(value.get("type") or "").strip().lower()
        if value_type in reasoning_types:
            own_text = value.get("text")
            if isinstance(own_text, str) and own_text:
                parts.append(own_text)
            own_content = value.get("content")
            if isinstance(own_content, str) and own_content:
                parts.append(own_content)
            elif isinstance(own_content, list):
                parts.append(OpenWebUIClient._extract_stream_reasoning(own_content))

        for key in reasoning_keys:
            key_value = value.get(key)
            if key_value is None:
                continue
            text = _normalize_content(key_value)
            if text:
                parts.append(text)

        content_value = value.get("content")
        if isinstance(content_value, list):
            for item in content_value:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in reasoning_types:
                    text = OpenWebUIClient._extract_stream_text(item.get("text") or item.get("content"))
                    if text:
                        parts.append(text)

        return "".join(parts)

    @staticmethod
    def _merge_stream_tool_calls(
        acc: dict[int, dict[str, Any]],
        order: list[int],
        raw_calls: Any,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not isinstance(raw_calls, list):
            return
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            index = call.get("index")
            if not isinstance(index, int):
                index = len(order)
            if index not in acc:
                acc[index] = {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
                order.append(index)

            target = acc[index]
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                target["id"] = call_id

            call_type = call.get("type")
            if isinstance(call_type, str) and call_type:
                target["type"] = call_type

            fn = call.get("function")
            if isinstance(fn, dict):
                fn_target = target.setdefault("function", {"name": "", "arguments": ""})
                name = fn.get("name")
                if isinstance(name, str) and name:
                    existing_name = str(fn_target.get("name") or "")
                    if not existing_name:
                        fn_target["name"] = name
                    elif name == existing_name or existing_name.endswith(name):
                        fn_target["name"] = existing_name
                    elif name.startswith(existing_name):
                        fn_target["name"] = name
                    else:
                        fn_target["name"] = f"{existing_name}{name}"
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    existing_args = str(fn_target.get("arguments") or "")
                    if not existing_args:
                        fn_target["arguments"] = args
                    elif args == existing_args or existing_args.endswith(args):
                        fn_target["arguments"] = existing_args
                    elif args.startswith(existing_args):
                        fn_target["arguments"] = args
                    else:
                        fn_target["arguments"] = f"{existing_args}{args}"

                if isinstance(fn_target.get("name"), str) and fn_target.get("name"):
                    OpenWebUIClient._notify_stream_event(
                        on_event,
                        {"type": "tool_call", "name": fn_target.get("name"), "id": target.get("id")},
                    )

    @staticmethod
    def _merge_stream_function_call(
        acc: dict[int, dict[str, Any]],
        order: list[int],
        raw_call: Any,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not isinstance(raw_call, dict):
            return
        OpenWebUIClient._merge_stream_tool_calls(
            acc,
            order,
            [{"index": 0, "type": "function", "function": raw_call}],
            on_event=on_event,
        )

    @staticmethod
    def _finalize_stream_tool_calls(
        acc: dict[int, dict[str, Any]],
        order: list[int],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index in order:
            raw_item = acc.get(index)
            if not isinstance(raw_item, dict):
                continue
            item = {
                "id": str(raw_item.get("id") or f"tool_call_{index}"),
                "type": str(raw_item.get("type") or "function"),
                "function": {
                    "name": str(((raw_item.get("function") or {}).get("name") or "")).strip(),
                    "arguments": str(((raw_item.get("function") or {}).get("arguments") or "")),
                },
            }
            if not item["function"]["name"]:
                continue
            items.append(item)
        return items

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
