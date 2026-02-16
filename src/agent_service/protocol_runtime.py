from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .service import AgentRuntime

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class ProtocolRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "runtime_error",
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


@dataclass(slots=True)
class SessionState:
    session_id: str
    model_id: str | None
    chat_id: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    active_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model_id": self.model_id,
            "chat_id": self.chat_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_run_id": self.active_run_id,
        }


@dataclass(slots=True)
class PromptRunState:
    run_id: str
    session_id: str
    message: str
    auto_apply: bool
    created_at: str
    status: str = "queued"
    started_at: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    task: asyncio.Task[dict[str, Any]] | None = None
    _stream_event_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "message": self.message,
            "auto_apply": self.auto_apply,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
        }


class AgentProtocolRuntime:
    def __init__(self, agent_runtime: AgentRuntime):
        self._agent_runtime = agent_runtime
        self._sessions: dict[str, SessionState] = {}
        self._runs: dict[str, PromptRunState] = {}
        self._lock = asyncio.Lock()

    def create_session(
        self,
        *,
        model_id: str | None = None,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now_iso()
        session_id = str(uuid4())
        self._sessions[session_id] = SessionState(
            session_id=session_id,
            model_id=_clean_optional_str(model_id),
            chat_id=_clean_optional_str(chat_id),
            metadata=_normalize_metadata(metadata),
            created_at=now,
            updated_at=now,
        )
        return self._sessions[session_id].to_dict()

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = [session.to_dict() for session in self._sessions.values()]
        rows.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return rows

    def get_session(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        return session.to_dict()

    def update_session(
        self,
        session_id: str,
        *,
        model_id: str | None = None,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._require_session(session_id)

        if model_id is not None:
            session.model_id = _clean_optional_str(model_id)
        if chat_id is not None:
            session.chat_id = _clean_optional_str(chat_id)
        if metadata is not None:
            session.metadata = _normalize_metadata(metadata)

        session.updated_at = _utc_now_iso()
        return session.to_dict()

    async def start_prompt(
        self,
        *,
        session_id: str,
        message: str,
        auto_apply: bool = True,
        on_event: EventCallback | None = None,
    ) -> str:
        clean_message = (message or "").strip()
        if not clean_message:
            raise ProtocolRuntimeError("message is required", code="invalid_params")

        async with self._lock:
            session = self._require_session(session_id)
            active_run = self._get_active_run(session)
            if active_run is not None:
                raise ProtocolRuntimeError(
                    "session already has active run",
                    code="run_in_progress",
                    data={"run_id": active_run.run_id},
                )

            run_id = str(uuid4())
            run = PromptRunState(
                run_id=run_id,
                session_id=session.session_id,
                message=clean_message,
                auto_apply=bool(auto_apply),
                created_at=_utc_now_iso(),
            )
            self._runs[run_id] = run
            session.active_run_id = run_id
            session.updated_at = _utc_now_iso()
            run.task = asyncio.create_task(
                self._execute_prompt_run(run=run, session=session, on_event=on_event),
                name=f"agent-protocol-run-{run_id}",
            )
            self._prune_runs(limit=256)
            return run_id

    def cancel_run(self, *, session_id: str, run_id: str | None = None) -> dict[str, Any]:
        session = self._require_session(session_id)
        target_run_id = _clean_optional_str(run_id) or session.active_run_id
        if not target_run_id:
            return {
                "session_id": session.session_id,
                "run_id": None,
                "cancelled": False,
                "reason": "no_active_run",
            }

        run = self._runs.get(target_run_id)
        if run is None:
            raise ProtocolRuntimeError("run not found", code="not_found", data={"run_id": target_run_id})

        if run.task is None:
            return {
                "session_id": session.session_id,
                "run_id": target_run_id,
                "cancelled": False,
                "reason": "run_task_missing",
            }

        if run.task.done():
            return {
                "session_id": session.session_id,
                "run_id": target_run_id,
                "cancelled": False,
                "reason": "run_already_finished",
            }

        run.task.cancel()
        return {
            "session_id": session.session_id,
            "run_id": target_run_id,
            "cancelled": True,
        }

    async def wait_run(self, run_id: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        run = self._require_run(run_id)
        if run.task is not None and not run.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(run.task), timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise ProtocolRuntimeError(
                    "run wait timed out",
                    code="timeout",
                    data={"run_id": run.run_id},
                ) from exc
        return run.to_dict()

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        return run.to_dict()

    async def _execute_prompt_run(
        self,
        *,
        run: PromptRunState,
        session: SessionState,
        on_event: EventCallback | None,
    ) -> dict[str, Any]:
        run.status = "running"
        run.started_at = _utc_now_iso()
        await self._emit_event(
            on_event,
            {
                "event": "run.started",
                "phase": "plan",
                "session_id": session.session_id,
                "run_id": run.run_id,
                "message": run.message,
                "auto_apply": run.auto_apply,
                "timestamp": _utc_now_iso(),
            },
        )

        loop = asyncio.get_running_loop()

        def stream_callback(event: dict[str, Any]) -> None:
            mapped = self._map_runtime_stream_event(
                run_id=run.run_id,
                session_id=session.session_id,
                source_event=event,
            )
            if mapped is None:
                return
            task = loop.create_task(self._emit_event(on_event, mapped))
            run._stream_event_tasks.append(task)

        try:
            raw_result = await self._agent_runtime.run_agent_task(
                message=run.message,
                model_id=session.model_id,
                chat_id=session.chat_id,
                auto_apply=run.auto_apply,
                stream_callback=stream_callback,
            )
            await self._drain_stream_event_tasks(run)

            public_result = self._public_agent_result(raw_result)
            run.result = public_result
            run.status = "completed"
            run.completed_at = _utc_now_iso()

            if public_result.get("chat_id"):
                session.chat_id = str(public_result.get("chat_id"))
            if public_result.get("model_id"):
                session.model_id = str(public_result.get("model_id"))
            session.updated_at = _utc_now_iso()

            verify_payload = self._build_verification_payload(public_result)
            await self._emit_event(
                on_event,
                {
                    "event": "run.verified",
                    "phase": "verify",
                    "session_id": session.session_id,
                    "run_id": run.run_id,
                    "payload": verify_payload,
                    "timestamp": _utc_now_iso(),
                },
            )
            await self._emit_event(
                on_event,
                {
                    "event": "run.completed",
                    "phase": "final",
                    "session_id": session.session_id,
                    "run_id": run.run_id,
                    "result": public_result,
                    "timestamp": _utc_now_iso(),
                },
            )
            return public_result
        except asyncio.CancelledError:
            await self._drain_stream_event_tasks(run)
            run.status = "cancelled"
            run.completed_at = _utc_now_iso()
            await self._emit_event(
                on_event,
                {
                    "event": "run.cancelled",
                    "phase": "final",
                    "session_id": session.session_id,
                    "run_id": run.run_id,
                    "timestamp": _utc_now_iso(),
                },
            )
            return {"cancelled": True}
        except Exception as exc:  # noqa: BLE001
            await self._drain_stream_event_tasks(run)
            run.status = "failed"
            run.completed_at = _utc_now_iso()
            run.error = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }
            await self._emit_event(
                on_event,
                {
                    "event": "run.failed",
                    "phase": "final",
                    "session_id": session.session_id,
                    "run_id": run.run_id,
                    "error": run.error,
                    "timestamp": _utc_now_iso(),
                },
            )
            return {"error": dict(run.error)}
        finally:
            if session.active_run_id == run.run_id:
                session.active_run_id = None
                session.updated_at = _utc_now_iso()

    @staticmethod
    async def _emit_event(on_event: EventCallback | None, payload: dict[str, Any]) -> None:
        if on_event is None:
            return
        try:
            value = on_event(payload)
            if value is not None and hasattr(value, "__await__"):
                await value
        except Exception:
            # Event delivery issues must not break run execution.
            return

    @staticmethod
    async def _drain_stream_event_tasks(run: PromptRunState) -> None:
        if not run._stream_event_tasks:
            return
        tasks = [task for task in run._stream_event_tasks if not task.done()]
        run._stream_event_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _map_runtime_stream_event(
        *,
        run_id: str,
        session_id: str,
        source_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(source_event, dict):
            return None
        event_type = str(source_event.get("type") or "").strip().lower() or "stream"
        phase = "plan"
        if event_type in {"tool_start", "tool_result"}:
            phase = "act"
        elif event_type in {"assistant_delta"}:
            phase = "final"
        elif event_type in {"reasoning_delta"}:
            phase = "plan"
        elif event_type == "status":
            text = str(source_event.get("text") or "").lower()
            if "выполняю" in text:
                phase = "act"
            elif "ответ сформирован" in text:
                phase = "final"

        return {
            "event": "run.progress",
            "phase": phase,
            "session_id": session_id,
            "run_id": run_id,
            "payload": source_event,
            "timestamp": _utc_now_iso(),
        }

    @staticmethod
    def _public_agent_result(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "chat_id": _clean_optional_str(payload.get("chat_id")),
            "model_id": _clean_optional_str(payload.get("model_id")),
            "assistant_message": str(payload.get("assistant_message") or ""),
            "applied_files": _clean_string_list(payload.get("applied_files")),
            "pending_id": _clean_optional_str(payload.get("pending_id")),
            "pending_changes": _clean_pending_changes(payload.get("pending_changes")),
            "tool_steps": _as_int(payload.get("tool_steps"), default=0),
            "chat_title": _clean_optional_str(payload.get("chat_title")),
        }

    @staticmethod
    def _build_verification_payload(result: dict[str, Any]) -> dict[str, Any]:
        applied_files = _clean_string_list(result.get("applied_files"))
        pending_changes = _clean_pending_changes(result.get("pending_changes"))
        assistant_message = str(result.get("assistant_message") or "").strip()
        tool_steps = _as_int(result.get("tool_steps"), default=0)

        return {
            "checks": {
                "assistant_message_present": bool(assistant_message),
                "tool_steps_recorded": tool_steps > 0,
                "changes_detected": bool(applied_files or pending_changes),
            },
            "tool_steps": tool_steps,
            "applied_files_count": len(applied_files),
            "pending_changes_count": len(pending_changes),
        }

    def _require_session(self, session_id: str) -> SessionState:
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            raise ProtocolRuntimeError("session_id is required", code="invalid_params")

        session = self._sessions.get(clean_session_id)
        if session is None:
            raise ProtocolRuntimeError(
                "session not found",
                code="not_found",
                data={"session_id": clean_session_id},
            )
        return session

    def _require_run(self, run_id: str) -> PromptRunState:
        clean_run_id = str(run_id or "").strip()
        if not clean_run_id:
            raise ProtocolRuntimeError("run_id is required", code="invalid_params")
        run = self._runs.get(clean_run_id)
        if run is None:
            raise ProtocolRuntimeError("run not found", code="not_found", data={"run_id": clean_run_id})
        return run

    def _get_active_run(self, session: SessionState) -> PromptRunState | None:
        active_run_id = session.active_run_id
        if not active_run_id:
            return None
        run = self._runs.get(active_run_id)
        if run is None:
            session.active_run_id = None
            return None
        if run.task is not None and run.task.done():
            session.active_run_id = None
            return None
        if run.status in {"completed", "failed", "cancelled"}:
            session.active_run_id = None
            return None
        return run

    def _prune_runs(self, *, limit: int) -> None:
        if len(self._runs) <= limit:
            return
        ordered = sorted(self._runs.values(), key=lambda item: item.created_at)
        while len(ordered) > limit:
            candidate = ordered.pop(0)
            session = self._sessions.get(candidate.session_id)
            if session is not None and session.active_run_id == candidate.run_id:
                continue
            self._runs.pop(candidate.run_id, None)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key_text] = item
            continue
        result[key_text] = str(item)
    return result


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _clean_pending_changes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        operation = _clean_optional_str(item.get("operation")) or "unknown"
        path = _clean_optional_str(item.get("path")) or ""
        diff = str(item.get("diff") or "")
        apply_args = item.get("apply_args")
        if not isinstance(apply_args, dict):
            apply_args = {}
        result.append(
            {
                "operation": operation,
                "path": path,
                "diff": diff,
                "apply_args": apply_args,
            }
        )
    return result
