from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .service import AgentRuntime
from .session_store import SessionStore

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
ToolPolicyCallback = Callable[[str, dict[str, Any]], dict[str, Any] | None]

_MUTATING_TOOLS = {"write_file", "replace_in_file", "append_to_file", "delete_file"}
_DEFAULT_VERIFY_TIMEOUT_SECONDS = 120.0
_VERIFY_OUTPUT_LIMIT = 6000


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
    tool_policy: dict[str, Any] = field(default_factory=dict)
    verify_commands: list[str] = field(default_factory=list)
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
            "tool_policy": dict(self.tool_policy),
            "verify_commands": list(self.verify_commands),
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
        }


class AgentProtocolRuntime:
    def __init__(
        self,
        agent_runtime: AgentRuntime,
        *,
        store: SessionStore | None = None,
        verify_commands: list[str] | None = None,
    ):
        self._agent_runtime = agent_runtime
        self._store = store
        self._default_verify_commands = _normalize_string_list(verify_commands)
        self._sessions: dict[str, SessionState] = {}
        self._runs: dict[str, PromptRunState] = {}
        self._lock = asyncio.Lock()
        self._load_persisted_state()

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
        self._persist_state()
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
        self._persist_state()
        return session.to_dict()

    async def start_prompt(
        self,
        *,
        session_id: str,
        message: str,
        auto_apply: bool = True,
        on_event: EventCallback | None = None,
        tool_policy: dict[str, Any] | None = None,
        verify_commands: list[str] | None = None,
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

            resolved_policy = self._resolve_tool_policy(session, override=tool_policy)
            resolved_verify_commands = self._resolve_verify_commands(session, override=verify_commands)
            run_id = str(uuid4())
            run = PromptRunState(
                run_id=run_id,
                session_id=session.session_id,
                message=clean_message,
                auto_apply=bool(auto_apply),
                created_at=_utc_now_iso(),
                tool_policy=resolved_policy,
                verify_commands=resolved_verify_commands,
            )
            self._runs[run_id] = run
            session.active_run_id = run_id
            session.updated_at = _utc_now_iso()
            self._persist_state()
            run.task = asyncio.create_task(
                self._execute_prompt_run(run=run, session=session, on_event=on_event),
                name=f"agent-protocol-run-{run_id}",
            )
            self._prune_runs(limit=256)
            self._persist_state()
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
        self._persist_state()
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
        self._persist_state()
        await self._emit_event(
            on_event,
            {
                "event": "run.started",
                "phase": "plan",
                "session_id": session.session_id,
                "run_id": run.run_id,
                "message": run.message,
                "auto_apply": run.auto_apply,
                "tool_policy": dict(run.tool_policy),
                "verify_commands": list(run.verify_commands),
                "timestamp": _utc_now_iso(),
            },
        )

        loop = asyncio.get_running_loop()

        def tool_policy_callback(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
            return self._decide_tool_policy(run.tool_policy, tool_name, tool_args)

        def stream_callback(event: dict[str, Any]) -> None:
            mapped_items = self._map_runtime_stream_events(
                run_id=run.run_id,
                session_id=session.session_id,
                source_event=event,
            )
            for mapped in mapped_items:
                task = loop.create_task(self._emit_event(on_event, mapped))
                run._stream_event_tasks.append(task)

        try:
            raw_result = await self._agent_runtime.run_agent_task(
                message=run.message,
                model_id=session.model_id,
                chat_id=session.chat_id,
                auto_apply=run.auto_apply,
                stream_callback=stream_callback,
                tool_policy=tool_policy_callback,
            )
            await self._drain_stream_event_tasks(run)

            public_result = self._public_agent_result(raw_result)
            verify_timeout = self._resolve_verify_timeout_seconds(session)
            workspace_checks = await self._run_workspace_verification(
                run.verify_commands,
                timeout_seconds=verify_timeout,
            )
            verify_payload = self._build_verification_payload(public_result, workspace_checks)
            public_result["verification"] = verify_payload

            run.result = public_result
            run.status = "completed"
            run.completed_at = _utc_now_iso()

            if public_result.get("chat_id"):
                session.chat_id = str(public_result.get("chat_id"))
            if public_result.get("model_id"):
                session.model_id = str(public_result.get("model_id"))
            session.updated_at = _utc_now_iso()
            self._persist_state()

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
            self._persist_state()
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
            self._persist_state()
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
            self._persist_state()

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
    def _map_runtime_stream_events(
        *,
        run_id: str,
        session_id: str,
        source_event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not isinstance(source_event, dict):
            return []
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

        events: list[dict[str, Any]] = [
            {
                "event": "run.progress",
                "phase": phase,
                "session_id": session_id,
                "run_id": run_id,
                "payload": source_event,
                "timestamp": _utc_now_iso(),
            }
        ]
        if event_type == "tool_result":
            tool_name = str(source_event.get("name") or "tool").strip() or "tool"
            policy_value = source_event.get("policy")
            policy = policy_value if isinstance(policy_value, dict) else {}
            decision = str(policy.get("decision") or "approve").strip().lower()
            if decision not in {"approve", "deny"}:
                decision = "approve"
            events.append(
                {
                    "event": "tool.result",
                    "phase": "act",
                    "session_id": session_id,
                    "run_id": run_id,
                    "tool": {
                        "name": tool_name,
                        "ok": bool(source_event.get("ok")),
                        "text": str(source_event.get("text") or ""),
                        "path": _clean_optional_str(source_event.get("path")),
                        "error": _clean_optional_str(source_event.get("error")),
                        "policy": {
                            "decision": decision,
                            "reason": str(policy.get("reason") or ""),
                            "source": str(policy.get("source") or "default"),
                        },
                    },
                    "timestamp": _utc_now_iso(),
                }
            )
        return events

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
    def _build_verification_payload(
        result: dict[str, Any],
        workspace_checks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        applied_files = _clean_string_list(result.get("applied_files"))
        pending_changes = _clean_pending_changes(result.get("pending_changes"))
        assistant_message = str(result.get("assistant_message") or "").strip()
        tool_steps = _as_int(result.get("tool_steps"), default=0)

        passed_checks = 0
        failed_checks = 0
        for item in workspace_checks:
            status = str(item.get("status") or "").strip().lower()
            if status == "passed":
                passed_checks += 1
            elif status:
                failed_checks += 1

        return {
            "checks": {
                "assistant_message_present": bool(assistant_message),
                "tool_steps_recorded": tool_steps > 0,
                "changes_detected": bool(applied_files or pending_changes),
                "workspace_checks_passed": failed_checks == 0 if workspace_checks else None,
            },
            "tool_steps": tool_steps,
            "applied_files_count": len(applied_files),
            "pending_changes_count": len(pending_changes),
            "workspace_checks": workspace_checks,
            "workspace_summary": {
                "total": len(workspace_checks),
                "passed": passed_checks,
                "failed": failed_checks,
            },
        }

    async def _run_workspace_verification(
        self,
        commands: list[str],
        *,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        clean_commands = _normalize_string_list(commands)
        if not clean_commands:
            return []

        cwd = self._resolve_project_root()
        rows: list[dict[str, Any]] = []
        for command in clean_commands:
            started = time.perf_counter()
            row: dict[str, Any] = {
                "command": command,
                "cwd": str(cwd),
                "status": "error",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "duration_ms": 0,
            }
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
                    row["status"] = "timeout"
                    row["stderr"] = f"timeout after {int(timeout_seconds)}s"
                else:
                    row["exit_code"] = process.returncode
                    row["stdout"] = _truncate_output(stdout.decode("utf-8", errors="replace"))
                    row["stderr"] = _truncate_output(stderr.decode("utf-8", errors="replace"))
                    row["status"] = "passed" if process.returncode == 0 else "failed"
            except Exception as exc:  # noqa: BLE001
                row["status"] = "error"
                row["stderr"] = _truncate_output(str(exc))
            finally:
                row["duration_ms"] = int((time.perf_counter() - started) * 1000)
            rows.append(row)
        return rows

    def _resolve_tool_policy(
        self,
        session: SessionState,
        *,
        override: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {}
        metadata_policy = session.metadata.get("tool_policy")
        if isinstance(metadata_policy, dict):
            base = dict(metadata_policy)
        if isinstance(override, dict):
            base.update(override)

        allow_tools = _normalize_string_list(base.get("allow_tools"))
        deny_tools = _normalize_string_list(base.get("deny_tools"))
        default_decision = str(base.get("default_decision") or "approve").strip().lower()
        if default_decision not in {"approve", "deny"}:
            default_decision = "approve"
        deny_mutations = bool(base.get("deny_mutations", False))
        return {
            "allow_tools": allow_tools,
            "deny_tools": deny_tools,
            "deny_mutations": deny_mutations,
            "default_decision": default_decision,
        }

    def _resolve_verify_commands(
        self,
        session: SessionState,
        *,
        override: list[str] | None,
    ) -> list[str]:
        if override is not None:
            return _normalize_string_list(override)
        meta_commands = session.metadata.get("verify_commands")
        if isinstance(meta_commands, list):
            return _normalize_string_list(meta_commands)
        return list(self._default_verify_commands)

    @staticmethod
    def _decide_tool_policy(
        policy: dict[str, Any],
        tool_name: str,
        _tool_args: dict[str, Any],
    ) -> dict[str, Any]:
        clean_name = str(tool_name or "").strip()
        allow_tools = set(_normalize_string_list(policy.get("allow_tools")))
        deny_tools = set(_normalize_string_list(policy.get("deny_tools")))
        deny_mutations = bool(policy.get("deny_mutations", False))
        default_decision = str(policy.get("default_decision") or "approve").strip().lower()
        if default_decision not in {"approve", "deny"}:
            default_decision = "approve"

        if clean_name in deny_tools:
            return {
                "decision": "deny",
                "reason": "tool is in deny_tools policy list",
                "source": "policy.deny_tools",
            }
        if deny_mutations and clean_name in _MUTATING_TOOLS:
            return {
                "decision": "deny",
                "reason": "mutating tools are denied by policy",
                "source": "policy.deny_mutations",
            }
        if allow_tools and clean_name not in allow_tools:
            return {
                "decision": "deny",
                "reason": "tool is not listed in allow_tools",
                "source": "policy.allow_tools",
            }
        if default_decision == "deny":
            return {
                "decision": "deny",
                "reason": "default_decision is deny",
                "source": "policy.default_decision",
            }
        return {
            "decision": "approve",
            "reason": "",
            "source": "policy.default",
        }

    def _resolve_verify_timeout_seconds(self, session: SessionState) -> float:
        raw = session.metadata.get("verify_timeout_seconds")
        if raw is None:
            return _DEFAULT_VERIFY_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return _DEFAULT_VERIFY_TIMEOUT_SECONDS
        if value <= 0:
            return _DEFAULT_VERIFY_TIMEOUT_SECONDS
        return value

    def _resolve_project_root(self) -> Path:
        if self._store is not None:
            return self._store.project_root
        project_root = getattr(self._agent_runtime, "project_root", None)
        if isinstance(project_root, Path):
            return project_root
        return Path.cwd()

    def _load_persisted_state(self) -> None:
        if self._store is None:
            return
        payload = self._store.load_protocol_state()
        sessions_value = payload.get("sessions")
        runs_value = payload.get("runs")
        sessions = sessions_value if isinstance(sessions_value, list) else []
        runs = runs_value if isinstance(runs_value, list) else []

        now = _utc_now_iso()
        changed = False
        for item in sessions:
            if not isinstance(item, dict):
                continue
            session_id = _clean_optional_str(item.get("session_id"))
            if not session_id:
                continue
            session = SessionState(
                session_id=session_id,
                model_id=_clean_optional_str(item.get("model_id")),
                chat_id=_clean_optional_str(item.get("chat_id")),
                metadata=_normalize_metadata(item.get("metadata")),
                created_at=str(item.get("created_at") or now),
                updated_at=str(item.get("updated_at") or now),
                active_run_id=_clean_optional_str(item.get("active_run_id")),
            )
            self._sessions[session_id] = session

        for item in runs:
            if not isinstance(item, dict):
                continue
            run_id = _clean_optional_str(item.get("run_id"))
            session_id = _clean_optional_str(item.get("session_id"))
            if not run_id or not session_id:
                continue
            status = str(item.get("status") or "queued")
            error_value = item.get("error")
            error = error_value if isinstance(error_value, dict) else None
            if status in {"queued", "running"}:
                status = "interrupted"
                error = {
                    "type": "Interrupted",
                    "message": "Run was interrupted by process restart",
                }
                changed = True
            run = PromptRunState(
                run_id=run_id,
                session_id=session_id,
                message=str(item.get("message") or ""),
                auto_apply=bool(item.get("auto_apply", True)),
                created_at=str(item.get("created_at") or now),
                tool_policy=_normalize_tool_policy(item.get("tool_policy")),
                verify_commands=_normalize_string_list(item.get("verify_commands")),
                status=status,
                started_at=_clean_optional_str(item.get("started_at")),
                completed_at=_clean_optional_str(item.get("completed_at")) or (now if status == "interrupted" else None),
                result=item.get("result") if isinstance(item.get("result"), dict) else None,
                error=error,
            )
            self._runs[run_id] = run

        for session in self._sessions.values():
            if not session.active_run_id:
                continue
            active_run = self._runs.get(session.active_run_id)
            if active_run is None or active_run.status in {"completed", "failed", "cancelled", "interrupted"}:
                session.active_run_id = None
                session.updated_at = now
                changed = True

        if changed:
            self._persist_state()

    def _persist_state(self) -> None:
        if self._store is None:
            return
        try:
            payload = {
                "sessions": [item.to_dict() for item in self._sessions.values()],
                "runs": [item.to_dict() for item in self._runs.values()],
            }
            self._store.save_protocol_state(payload)
        except Exception:
            return

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
        if run.status in {"completed", "failed", "cancelled", "interrupted"}:
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


def _normalize_metadata(value: Any) -> dict[str, Any]:
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
        if isinstance(item, list):
            result[key_text] = [str(part) for part in item]
            continue
        if isinstance(item, dict):
            result[key_text] = dict(item)
            continue
        result[key_text] = str(item)
    return result


def _normalize_tool_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "allow_tools": _normalize_string_list(value.get("allow_tools")),
        "deny_tools": _normalize_string_list(value.get("deny_tools")),
        "deny_mutations": bool(value.get("deny_mutations", False)),
        "default_decision": (
            str(value.get("default_decision") or "approve").strip().lower()
            if str(value.get("default_decision") or "approve").strip().lower() in {"approve", "deny"}
            else "approve"
        ),
    }


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _clean_string_list(value: Any) -> list[str]:
    return _normalize_string_list(value)


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


def _truncate_output(value: str) -> str:
    text = str(value or "")
    if len(text) <= _VERIFY_OUTPUT_LIMIT:
        return text
    return f"{text[:_VERIFY_OUTPUT_LIMIT]}...[truncated]"
