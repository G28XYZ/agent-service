from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .config import AppConfig
from .openwebui_client import (
    AuthenticationError,
    ModelNotFoundError,
    OpenWebUIClient,
    RequestFailedError,
)
from .session_store import SessionStore
from .workspace_tools import WorkspaceToolError, WorkspaceTools

_AGENT_MAX_STEPS = 8
_AGENT_HISTORY_LIMIT = 24
_FALLBACK_REPAIR_ATTEMPTS = 6
_SUPPORTED_TOOL_NAMES = {
    "list_files",
    "read_file",
    "search_in_files",
    "write_file",
    "replace_in_file",
    "delete_file",
}
_MUTATING_TOOL_NAMES = {"write_file", "replace_in_file", "delete_file"}
_TOOL_NAME_ALIASES = {
    "create_file": "write_file",
    "append_file": "write_file",
}
_ARG_NAME_ALIASES = {
    "file_path": "path",
    "filepath": "path",
    "filename": "path",
    "text": "content",
}
_AGENT_SYSTEM_PROMPT = (
    "You are Agent, a local coding assistant. "
    "You can inspect and update files only with the provided tools. "
    "First gather context with list/read/search tools, then apply focused edits. "
    "Do not replace whole existing files when a targeted edit is sufficient. "
    "Prefer replace_in_file for updates to existing files. "
    "Prefer minimal safe changes. "
    "After tools are done, answer with a concise summary."
)
_REASONING_TAG_PATTERNS = (
    re.compile(r"<think\b[^>]*>.*?</think>", flags=re.IGNORECASE | re.DOTALL),
    re.compile(r"<analysis\b[^>]*>.*?</analysis>", flags=re.IGNORECASE | re.DOTALL),
)
_REASONING_FENCE_PATTERN = re.compile(
    r"```(?:thinking|reasoning|analysis)[\w -]*\n.*?```",
    flags=re.IGNORECASE | re.DOTALL,
)


class AgentRuntime:
    def __init__(self, config: AppConfig, store: SessionStore, client: OpenWebUIClient):
        self._config = config
        self._store = store
        self._client = client
        self._agent_memory: dict[str, list[dict[str, str]]] = {}
        self._pending_change_sets: dict[str, list[dict[str, Any]]] = {}
        self._applied_change_sets: dict[str, list[dict[str, Any]]] = {}

    async def startup(self) -> None:
        await self._client.startup()

    async def shutdown(self) -> None:
        await self._client.shutdown()

    async def login(self, username: str | None = None, password: str | None = None) -> dict[str, Any]:
        resolved_username, resolved_password = self._resolve_credentials(username, password)
        details = await self._client.login(resolved_username, resolved_password)

        return {
            "authenticated": True,
            "username": resolved_username,
            "details": details,
        }

    async def auth_status(self) -> dict[str, Any]:
        authenticated, details = await self._client.session_check()

        auth_snapshot = self._store.load_auth()
        auth_snapshot["last_session_check"] = _utc_now_iso()
        auth_snapshot["authenticated"] = authenticated
        self._store.save_auth(auth_snapshot)

        return {
            "authenticated": authenticated,
            "details": details,
        }

    async def list_models(self) -> list[dict[str, Any]]:
        await self._ensure_authenticated()

        try:
            return await self._client.list_models()
        except AuthenticationError:
            await self.login()
            return await self._client.list_models()

    async def list_chats(self) -> list[dict[str, Any]]:
        local = self._store.list_chats()
        remote: list[dict[str, Any]] = []
        try:
            await self._ensure_authenticated()
            remote = await self._client.list_chats()
        except AuthenticationError:
            await self.login()
            remote = await self._client.list_chats()
        except RequestFailedError:
            remote = []

        merged = self._merge_chats(remote, local)
        return merged

    async def create_chat(self, model_id: str | None, title: str | None = None) -> dict[str, Any]:
        selected_model = await self.resolve_model(model_id)
        await self._ensure_authenticated()

        try:
            raw = await self._client.create_chat(selected_model, title)
        except AuthenticationError:
            await self.login()
            raw = await self._client.create_chat(selected_model, title)

        created_at = _utc_now_iso()
        chat_id = self._extract_chat_id(raw) or str(uuid4())
        record = {
            "chat_id": chat_id,
            "model_id": selected_model,
            "title": title,
            "created_at": created_at,
        }
        self._store.record_chat(record, autobind=self._config.agent.project_chat_autobind)

        return {
            "chat_id": chat_id,
            "model_id": selected_model,
            "created_at": created_at,
            "raw": raw,
        }

    def set_active_chat(self, chat_id: str) -> None:
        if not chat_id:
            raise ValueError("chat_id is required")
        self._store.set_latest_chat(chat_id, autobind=self._config.agent.project_chat_autobind)

    async def send_message(
        self,
        message: str,
        model_id: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        clean_message = (message or "").strip()
        if not clean_message:
            raise ValueError("message must not be empty")

        selected_model = await self.resolve_model(model_id)
        await self._ensure_authenticated()

        try:
            raw = await self._client.send_message(
                model_id=selected_model,
                message=clean_message,
                chat_id=chat_id,
            )
        except AuthenticationError:
            await self.login()
            raw = await self._client.send_message(
                model_id=selected_model,
                message=clean_message,
                chat_id=chat_id,
            )

        resolved_chat_id = chat_id or self._extract_chat_id(raw)
        assistant_message = self._extract_assistant_text(raw)
        self._remember_chat_turn(resolved_chat_id, clean_message, assistant_message)

        return {
            "chat_id": resolved_chat_id,
            "model_id": selected_model,
            "assistant_message": assistant_message,
            "raw": raw,
        }

    async def run_agent_task(
        self,
        message: str,
        model_id: str | None = None,
        chat_id: str | None = None,
        *,
        auto_apply: bool = True,
        stream_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        clean_message = (message or "").strip()
        if not clean_message:
            raise ValueError("message must not be empty")
        task_requires_changes = self._task_requires_file_changes(clean_message)

        selected_model = await self.resolve_model(model_id)
        await self._ensure_authenticated()

        workspace = WorkspaceTools(self._store.project_root)
        resolved_chat_id, resolved_chat_title = await self._ensure_chat_for_task(
            selected_model,
            chat_id,
            clean_message,
        )
        history_messages = await self._load_chat_history_context(resolved_chat_id)
        if not history_messages and resolved_chat_id:
            history_messages = list(self._agent_memory.get(resolved_chat_id, []))
        conversation: list[dict[str, Any]] = [{"role": "system", "content": _AGENT_SYSTEM_PROMPT}]
        conversation.extend(history_messages)
        conversation.append({"role": "user", "content": clean_message})
        tool_definitions = WorkspaceTools.tool_definitions()
        applied_files: list[str] = []
        pending_changes: list[dict[str, Any]] = []
        total_tool_calls = 0
        last_raw: dict[str, Any] = {}

        for step_idx in range(_AGENT_MAX_STEPS):
            self._emit_stream_event(
                stream_callback,
                {
                    "type": "status",
                    "text": f"Шаг {step_idx + 1}: запрос к модели",
                },
                chat_id=resolved_chat_id,
                model_id=selected_model,
                step=step_idx + 1,
            )
            try:
                raw = await self._client.chat_completion_stream(
                    model_id=selected_model,
                    messages=conversation,
                    chat_id=resolved_chat_id,
                    tools=tool_definitions,
                    on_event=lambda event, step=step_idx + 1: self._emit_stream_event(
                        stream_callback,
                        event,
                        chat_id=resolved_chat_id,
                        model_id=selected_model,
                        step=step,
                    ),
                )
            except AuthenticationError:
                await self.login()
                raw = await self._client.chat_completion_stream(
                    model_id=selected_model,
                    messages=conversation,
                    chat_id=resolved_chat_id,
                    tools=tool_definitions,
                    on_event=lambda event, step=step_idx + 1: self._emit_stream_event(
                        stream_callback,
                        event,
                        chat_id=resolved_chat_id,
                        model_id=selected_model,
                        step=step,
                    ),
                )
            except RequestFailedError as exc:
                if exc.status_code not in {400, 404, 422}:
                    raise
                # Some providers reject stream=true payload but accept the same request in non-stream mode.
                try:
                    raw = await self._client.chat_completion(
                        model_id=selected_model,
                        messages=conversation,
                        chat_id=resolved_chat_id,
                        tools=tool_definitions,
                    )
                except AuthenticationError:
                    await self.login()
                    raw = await self._client.chat_completion(
                        model_id=selected_model,
                        messages=conversation,
                        chat_id=resolved_chat_id,
                        tools=tool_definitions,
                    )
                except RequestFailedError as non_stream_exc:
                    if non_stream_exc.status_code in {400, 404, 422} and total_tool_calls == 0:
                        self._emit_stream_event(
                            stream_callback,
                            {
                                "type": "status",
                                "text": "Модель не поддерживает tool-calls в текущем формате, fallback режим",
                            },
                            chat_id=resolved_chat_id,
                            model_id=selected_model,
                            step=step_idx + 1,
                        )
                        fallback = await self._fallback_agent_to_message(
                            selected_model=selected_model,
                            clean_message=clean_message,
                            chat_id=resolved_chat_id,
                            reason=str(non_stream_exc),
                            workspace=workspace,
                            auto_apply=auto_apply,
                            history_messages=history_messages,
                        )
                        fallback["chat_title"] = resolved_chat_title
                        self._remember_chat_turn(
                            fallback.get("chat_id"),
                            clean_message,
                            str(fallback.get("assistant_message") or ""),
                        )
                        return fallback
                    raise
                else:
                    self._emit_stream_event(
                        stream_callback,
                        {
                            "type": "status",
                            "text": "Потоковый ответ недоступен, продолжаю без стрима",
                        },
                        chat_id=resolved_chat_id,
                        model_id=selected_model,
                        step=step_idx + 1,
                    )

            last_raw = raw if isinstance(raw, dict) else {"value": raw}
            if resolved_chat_id is None:
                resolved_chat_id = chat_id or self._extract_chat_id(last_raw)

            assistant_text, tool_calls = self._extract_assistant_turn(last_raw)
            if not tool_calls:
                should_try_fallback = (
                    total_tool_calls == 0
                    or (task_requires_changes and not applied_files and not pending_changes)
                )
                if should_try_fallback:
                    fallback_reason = (
                        "tool_calls missing in assistant response"
                        if total_tool_calls == 0
                        else "assistant finished without file changes"
                    )
                    try:
                        fallback = await self._fallback_agent_to_message(
                            selected_model=selected_model,
                            clean_message=clean_message,
                            chat_id=resolved_chat_id,
                            reason=fallback_reason,
                            workspace=workspace,
                            auto_apply=auto_apply,
                            history_messages=history_messages,
                        )
                        fallback["chat_title"] = resolved_chat_title
                    except RequestFailedError:
                        fallback = None

                    fallback_has_effect = bool(
                        isinstance(fallback, dict)
                        and (
                            int(fallback.get("tool_steps") or 0) > 0
                            or bool(fallback.get("applied_files"))
                            or bool(fallback.get("pending_changes"))
                        )
                    )
                    if fallback_has_effect and isinstance(fallback, dict):
                        fallback_chat_id = str(fallback.get("chat_id") or resolved_chat_id or "").strip()
                        self._remember_chat_turn(
                            fallback_chat_id,
                            clean_message,
                            str(fallback.get("assistant_message") or ""),
                        )
                        return fallback

                final_text = assistant_text or self._extract_assistant_text(last_raw)
                if applied_files:
                    changed_lines = "\n".join(f"- {path}" for path in applied_files)
                    final_text = f"{final_text}\n\nUpdated files:\n{changed_lines}"
                if task_requires_changes and not applied_files and not pending_changes:
                    final_text = (
                        f"{final_text}\n\n"
                        "[Внимание: изменений в файлах не выполнено. "
                        "Сформулируйте точечную правку или разрешите rewrite конкретного файла.]"
                    )
                self._emit_stream_event(
                    stream_callback,
                    {"type": "status", "text": "Ответ сформирован"},
                    chat_id=resolved_chat_id,
                    model_id=selected_model,
                    step=step_idx + 1,
                )
                self._remember_chat_turn(resolved_chat_id, clean_message, final_text)
                pending_id = self._register_pending_changes(pending_changes)
                return {
                    "chat_id": resolved_chat_id,
                    "model_id": selected_model,
                    "assistant_message": final_text,
                    "raw": last_raw,
                    "applied_files": applied_files,
                    "pending_id": pending_id,
                    "pending_changes": pending_changes,
                    "tool_steps": total_tool_calls,
                    "chat_title": resolved_chat_title,
                }

            assistant_turn: dict[str, Any] = {
                "role": "assistant",
                "content": assistant_text or "",
                "tool_calls": tool_calls,
            }
            conversation.append(assistant_turn)

            for tool_call in tool_calls:
                total_tool_calls += 1
                tool_name = self._extract_tool_name(tool_call)
                tool_args = self._extract_tool_args(tool_call)
                self._emit_stream_event(
                    stream_callback,
                    {
                        "type": "tool_start",
                        "name": tool_name or "tool",
                        "args": tool_args,
                        "text": f"Выполняю {tool_name or 'tool'}",
                    },
                    chat_id=resolved_chat_id,
                    model_id=selected_model,
                    step=step_idx + 1,
                )
                tool_payload = self._execute_tool_call(
                    workspace,
                    tool_call,
                    auto_apply=auto_apply,
                )
                self._emit_stream_event(
                    stream_callback,
                    self._tool_result_stream_event(tool_payload),
                    chat_id=resolved_chat_id,
                    model_id=selected_model,
                    step=step_idx + 1,
                )
                changed_path = self._extract_changed_path(tool_payload)
                if changed_path and changed_path not in applied_files:
                    applied_files.append(changed_path)
                pending_change = self._extract_pending_change(tool_payload)
                if pending_change:
                    pending_changes.append(pending_change)
                conversation.append(self._build_tool_message(tool_call, tool_payload, total_tool_calls))

        timeout_summary = "Agent reached max tool iterations without final answer."
        if applied_files:
            changed_lines = "\n".join(f"- {path}" for path in applied_files)
            timeout_summary = f"{timeout_summary}\n\nUpdated files:\n{changed_lines}"
        if pending_changes:
            timeout_summary = f"{timeout_summary}\n\n{self._summarize_pending_changes(pending_changes)}"
        self._remember_chat_turn(resolved_chat_id, clean_message, timeout_summary)
        pending_id = self._register_pending_changes(pending_changes)
        return {
            "chat_id": resolved_chat_id,
            "model_id": selected_model,
            "assistant_message": timeout_summary,
            "raw": last_raw,
            "applied_files": applied_files,
            "pending_id": pending_id,
            "pending_changes": pending_changes,
            "tool_steps": total_tool_calls,
            "chat_title": resolved_chat_title,
        }

    def _remember_chat_turn(
        self,
        chat_id: Any,
        user_text: str,
        assistant_text: str,
    ) -> None:
        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            return

        memory = self._agent_memory.setdefault(clean_chat_id, [])
        clean_user = (user_text or "").strip()
        clean_assistant = _strip_reasoning_content(assistant_text or "")
        if clean_user:
            memory.append({"role": "user", "content": clean_user})
        if clean_assistant:
            memory.append({"role": "assistant", "content": clean_assistant})
        if len(memory) > _AGENT_HISTORY_LIMIT:
            self._agent_memory[clean_chat_id] = memory[-_AGENT_HISTORY_LIMIT:]
        self._store.append_chat_turn(clean_chat_id, clean_user, clean_assistant)

    async def _fallback_agent_to_message(
        self,
        *,
        selected_model: str,
        clean_message: str,
        chat_id: str | None,
        reason: str,
        workspace: WorkspaceTools,
        auto_apply: bool,
        history_messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        prompt = self._build_text_tools_prompt(clean_message, history_messages)
        raw: dict[str, Any] = {}
        resolved_chat_id = chat_id
        assistant_text = ""
        actions: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        applied_files: list[str] = []
        pending_changes: list[dict[str, Any]] = []
        cumulative_tool_results: list[dict[str, Any]] = []
        cumulative_applied_files: list[str] = []
        cumulative_pending_changes: list[dict[str, Any]] = []
        cumulative_pending_markers: set[str] = set()
        task_requires_changes = self._task_requires_file_changes(clean_message)

        repair_prompt = prompt
        for attempt in range(_FALLBACK_REPAIR_ATTEMPTS + 1):
            try:
                raw = await self._client.send_message(
                    model_id=selected_model,
                    message=repair_prompt,
                    chat_id=resolved_chat_id,
                )
            except AuthenticationError:
                await self.login()
                raw = await self._client.send_message(
                    model_id=selected_model,
                    message=repair_prompt,
                    chat_id=resolved_chat_id,
                )

            if resolved_chat_id is None:
                resolved_chat_id = chat_id or self._extract_chat_id(raw)

            assistant_text = self._extract_assistant_text(raw)
            actions = self._parse_text_tool_actions(assistant_text)
            tool_results = []
            applied_files = []
            pending_changes = []
            if actions:
                has_mutating_actions = False
                for idx, action in enumerate(actions, start=1):
                    if not isinstance(action, dict):
                        continue
                    raw_tool_name = str(action.get("tool") or action.get("name") or "").strip()
                    tool_name = _TOOL_NAME_ALIASES.get(raw_tool_name, raw_tool_name)
                    if not tool_name:
                        continue
                    if tool_name in _MUTATING_TOOL_NAMES:
                        has_mutating_actions = True
                    args_value = action.get("args")
                    if not isinstance(args_value, dict):
                        arguments = action.get("arguments")
                        args_value = arguments if isinstance(arguments, dict) else {}
                    if tool_name == "replace_in_file" and "replace" not in args_value:
                        args_value = dict(args_value)
                        args_value["replace"] = ""
                    tool_call = {
                        "id": f"text_tool_{idx}",
                        "function": {
                            "name": tool_name,
                            "arguments": args_value,
                        },
                    }
                    payload = self._execute_tool_call(workspace, tool_call, auto_apply=auto_apply)
                    tool_results.append(payload)
                    changed = self._extract_changed_path(payload)
                    if changed and changed not in applied_files:
                        applied_files.append(changed)
                    pending_change = self._extract_pending_change(payload)
                    if pending_change:
                        pending_changes.append(pending_change)
                if (
                    task_requires_changes
                    and actions
                    and not has_mutating_actions
                    and attempt < _FALLBACK_REPAIR_ATTEMPTS
                ):
                    repair_prompt = (
                        self._build_fallback_repair_prompt(
                            clean_message=clean_message,
                            history_messages=history_messages,
                            previous_actions=actions,
                            tool_results=cumulative_tool_results or tool_results,
                        )
                        + "\n\nYour previous response had no file-changing actions. "
                        "Now include at least one write_file/replace_in_file/delete_file action."
                    )
                    continue

            if tool_results:
                cumulative_tool_results.extend(tool_results)
                # Keep bounded history to avoid excessive prompt growth.
                if len(cumulative_tool_results) > 48:
                    cumulative_tool_results = cumulative_tool_results[-48:]
            for path in applied_files:
                if path not in cumulative_applied_files:
                    cumulative_applied_files.append(path)
            for change in pending_changes:
                if not isinstance(change, dict):
                    continue
                marker = json.dumps(
                    {
                        "operation": change.get("operation"),
                        "path": change.get("path"),
                        "apply_args": change.get("apply_args"),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
                if marker not in cumulative_pending_markers:
                    cumulative_pending_changes.append(change)
                    cumulative_pending_markers.add(marker)

            has_effect = bool(applied_files or pending_changes)
            if has_effect or attempt >= _FALLBACK_REPAIR_ATTEMPTS:
                break

            repair_prompt = self._build_fallback_repair_prompt(
                clean_message=clean_message,
                history_messages=history_messages,
                previous_actions=actions,
                tool_results=cumulative_tool_results or tool_results,
            )

        final_applied_files = cumulative_applied_files or applied_files
        final_pending_changes = cumulative_pending_changes or pending_changes
        final_tool_results = cumulative_tool_results or tool_results

        if final_applied_files:
            changed_lines = "\n".join(f"- {path}" for path in final_applied_files)
            final_text = f"Изменения применены (fallback mode):\n{changed_lines}"
        elif final_tool_results:
            final_text = self._summarize_tool_results(final_tool_results)
        else:
            note = (
                "\n\n[agent mode fallback: tool calling is not supported by current "
                "OpenWebUI/model payload format]"
            )
            final_text = f"{assistant_text}{note}"
        if task_requires_changes and not final_applied_files and not final_pending_changes:
            final_text = (
                f"{final_text}\n\n"
                "[Внимание: fallback не смог выполнить изменения в файлах. "
                "Попробуйте запросить точечный replace по конкретному фрагменту.]"
            )
        if final_pending_changes:
            final_text = f"{final_text}\n\n{self._summarize_pending_changes(final_pending_changes)}"
        pending_id = self._register_pending_changes(final_pending_changes)
        return {
            "chat_id": resolved_chat_id,
            "model_id": selected_model,
            "assistant_message": final_text,
            "raw": {
                "agent_fallback": True,
                "fallback_reason": reason,
                "fallback_response": raw,
                "parsed_actions": actions,
                "tool_results": final_tool_results,
            },
            "applied_files": final_applied_files,
            "pending_id": pending_id,
            "pending_changes": final_pending_changes,
            "tool_steps": len(final_tool_results),
        }

    async def _ensure_chat_for_task(
        self,
        selected_model: str,
        requested_chat_id: str | None,
        initial_message: str,
    ) -> tuple[str | None, str | None]:
        clean_requested = (requested_chat_id or "").strip()
        if clean_requested:
            return clean_requested, None

        await self._ensure_authenticated()
        generated_title = self._generate_chat_title_from_message(initial_message)
        try:
            raw = await self._client.create_chat(selected_model, generated_title)
        except AuthenticationError:
            await self.login()
            raw = await self._client.create_chat(selected_model, generated_title)
        except RequestFailedError:
            return None, None

        created_at = _utc_now_iso()
        chat_id = self._extract_chat_id(raw) or str(uuid4())
        record = {
            "chat_id": chat_id,
            "model_id": selected_model,
            "title": generated_title,
            "created_at": created_at,
        }
        self._store.record_chat(record, autobind=self._config.agent.project_chat_autobind)
        return chat_id, generated_title

    @staticmethod
    def _generate_chat_title_from_message(message: str) -> str:
        clean = re.sub(r"\s+", " ", str(message or "").strip())
        if not clean:
            return "Agent session"
        if len(clean) > 52:
            return f"{clean[:49].rstrip()}..."
        return clean

    async def _load_chat_history_context(self, chat_id: str | None) -> list[dict[str, str]]:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            return []

        local_history = self._normalize_history_messages(
            self._store.list_chat_messages(clean_chat_id, limit=_AGENT_HISTORY_LIMIT),
            limit=_AGENT_HISTORY_LIMIT,
        )
        try:
            history = await self._client.get_chat_history(clean_chat_id)
        except AuthenticationError:
            await self.login()
            try:
                history = await self._client.get_chat_history(clean_chat_id)
            except RequestFailedError:
                return local_history
        except RequestFailedError:
            return local_history

        normalized = self._normalize_history_messages(history, limit=_AGENT_HISTORY_LIMIT)
        if normalized:
            self._store.replace_chat_messages(clean_chat_id, normalized)
            return normalized
        return local_history

    @staticmethod
    def _build_text_tools_prompt(user_message: str, history_messages: list[dict[str, str]]) -> str:
        history_lines: list[str] = []
        for item in history_messages[-8:]:
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            label = "User" if role == "user" else "Agent"
            history_lines.append(f"{label}: {content[:400]}")
        history_block = "\n".join(history_lines)
        if history_block:
            history_block = f"Conversation context:\n{history_block}\n\n"
        return (
            "Tool calling is unavailable. Respond ONLY with JSON actions.\n"
            "Do not ask the user to run commands manually.\n"
            "If request is unclear, first call list_files with path='.'.\n"
            "For existing files, prefer replace_in_file over write_file.\n"
            "Schema:\n"
            "{\n"
            '  "actions": [\n'
            "    {\n"
            '      "tool": "list_files|read_file|search_in_files|write_file|replace_in_file|delete_file",\n'
            '      "args": { ... }\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Examples:\n"
            '- user: "какие файлы есть?" -> {"actions":[{"tool":"list_files","args":{"path":".","limit":200}}]}\n'
            '- user: "прочитай README.md" -> {"actions":[{"tool":"read_file","args":{"path":"README.md"}}]}\n'
            '- user: "найди TODO" -> {"actions":[{"tool":"search_in_files","args":{"query":"TODO","path":"."}}]}\n'
            "No markdown, no explanations.\n\n"
            f"{history_block}"
            f"User task:\n{user_message}"
        )

    @staticmethod
    def _build_fallback_repair_prompt(
        *,
        clean_message: str,
        history_messages: list[dict[str, str]],
        previous_actions: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> str:
        history_lines: list[str] = []
        for item in history_messages[-6:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            label = "User" if role == "user" else "Agent"
            history_lines.append(f"{label}: {content[:300]}")
        history_block = "\n".join(history_lines)
        if history_block:
            history_block = f"Conversation context:\n{history_block}\n\n"

        failed_lines: list[str] = []
        for item in tool_results:
            if not isinstance(item, dict):
                continue
            if item.get("ok"):
                continue
            name = str(item.get("name") or "tool")
            error = str(item.get("error") or "unknown error")
            failed_lines.append(f"- {name}: {error}")
        failed_block = "\n".join(failed_lines) if failed_lines else "- none"
        observations_block = AgentRuntime._tool_observations_for_prompt(tool_results)
        if not observations_block:
            observations_block = "- none"

        actions_preview = json.dumps(previous_actions, ensure_ascii=True)
        return (
            "Previous tool plan did not finish the task. Return corrected JSON actions only.\n"
            "Rules:\n"
            "1) For replace_in_file, always provide path/find/replace.\n"
            "2) If you do not know exact text, first call read_file(path=...) and then write_file/replace_in_file.\n"
            "3) Do not ask user for manual terminal commands.\n"
            "4) Keep actions minimal and executable.\n"
            "5) Do not overwrite existing non-empty files with write_file unless explicitly needed.\n"
            "6) If task asks for code changes/tests/refactor/fix, include write_file/replace_in_file/delete_file actions.\n\n"
            f"{history_block}"
            f"Original user task:\n{clean_message}\n\n"
            f"Previous actions:\n{actions_preview}\n\n"
            f"Tool errors:\n{failed_block}\n\n"
            f"Tool observations:\n{observations_block}\n\n"
            "If there are no tool errors but no file edits yet, continue with next actions using the observed file content.\n\n"
            "Response schema:\n"
            "{\n"
            '  "actions": [\n'
            "    {\n"
            '      "tool": "list_files|read_file|search_in_files|write_file|replace_in_file|delete_file",\n'
            '      "args": { ... }\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "No markdown, no explanations."
        )

    @staticmethod
    def _tool_observations_for_prompt(tool_results: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        budget = 2600
        for payload in tool_results:
            if budget <= 0:
                break
            if not isinstance(payload, dict):
                continue

            tool_name = str(payload.get("name") or "tool")
            if not payload.get("ok"):
                error = str(payload.get("error") or "unknown error")
                chunk = f"- {tool_name}: error: {error}\n"
                if len(chunk) > budget:
                    chunk = chunk[:budget]
                lines.append(chunk.rstrip("\n"))
                budget -= len(chunk)
                continue

            result = payload.get("result")
            if not isinstance(result, dict):
                chunk = f"- {tool_name}: ok\n"
                if len(chunk) > budget:
                    chunk = chunk[:budget]
                lines.append(chunk.rstrip("\n"))
                budget -= len(chunk)
                continue

            if tool_name == "read_file":
                path = str(result.get("path") or "")
                content = str(result.get("content") or "")
                content = content[:900]
                chunk = f"- read_file {path}:\n{content}\n"
            elif tool_name == "list_files":
                files = result.get("files")
                if isinstance(files, list):
                    preview = "\n".join(str(item) for item in files[:40])
                    chunk = f"- list_files:\n{preview}\n"
                else:
                    chunk = "- list_files: ok\n"
            elif tool_name == "search_in_files":
                matches = result.get("matches")
                if isinstance(matches, list):
                    rows: list[str] = []
                    for row in matches[:20]:
                        if not isinstance(row, dict):
                            continue
                        rows.append(
                            f"{row.get('path')}:{row.get('line')}: {str(row.get('text') or '').strip()}"
                        )
                    chunk = "- search_in_files:\n" + "\n".join(rows) + "\n"
                else:
                    chunk = "- search_in_files: ok\n"
            else:
                compact = json.dumps(result, ensure_ascii=True)
                chunk = f"- {tool_name}: {compact[:500]}\n"

            if len(chunk) > budget:
                chunk = chunk[:budget]
            lines.append(chunk.rstrip("\n"))
            budget -= len(chunk)

        return "\n".join(lines).strip()

    @staticmethod
    def _summarize_tool_results(tool_results: list[dict[str, Any]]) -> str:
        lines: list[str] = ["Инструменты выполнены (fallback mode):"]
        for payload in tool_results:
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name") or "tool")
            if not payload.get("ok"):
                lines.append(f"- {name}: error: {payload.get('error', 'unknown error')}")
                continue

            result = payload.get("result")
            if isinstance(result, dict) and result.get("applied") is False and result.get("changed"):
                path = str(result.get("path") or "")
                diff_text = str(result.get("diff") or "")
                additions, deletions = AgentRuntime._count_diff_changes(diff_text)
                lines.append(f"- {name}: pending {path} (+{additions} -{deletions})")
                continue

            if isinstance(result, dict) and result.get("applied") is False:
                path = str(result.get("path") or "")
                lines.append(f"- {name}: pending {path}")
                diff_text = str(result.get("diff") or "").strip()
                if diff_text:
                    additions, deletions = AgentRuntime._count_diff_changes(diff_text)
                    lines.append(f"  (+{additions} -{deletions})")
                continue

            if name == "list_files" and isinstance(result, dict):
                files = result.get("files")
                if isinstance(files, list):
                    lines.append(f"- list_files: found {len(files)} files")
                    preview = [str(item) for item in files[:40]]
                    lines.extend(preview)
                    if len(files) > len(preview):
                        lines.append(f"... (+{len(files) - len(preview)} more)")
                    continue

            if name == "read_file" and isinstance(result, dict):
                path = str(result.get("path") or "")
                content = str(result.get("content") or "")
                truncated = bool(result.get("truncated"))
                marker = " (truncated)" if truncated else ""
                lines.append(f"- read_file: {path}{marker}")
                if content:
                    lines.append(content[:2000])
                continue

            if name == "search_in_files" and isinstance(result, dict):
                matches = result.get("matches")
                if isinstance(matches, list):
                    lines.append(f"- search_in_files: {len(matches)} matches")
                    for row in matches[:30]:
                        if not isinstance(row, dict):
                            continue
                        path = str(row.get("path") or "")
                        line_no = row.get("line")
                        text = str(row.get("text") or "").strip()
                        lines.append(f"{path}:{line_no}: {text}")
                    if len(matches) > 30:
                        lines.append(f"... (+{len(matches) - 30} more matches)")
                    continue

            if isinstance(result, dict):
                changed = bool(result.get("changed"))
                path = str(result.get("path") or "")
                if changed and path:
                    lines.append(f"- {name}: changed {path}")
                    continue
            lines.append(f"- {name}: ok")

        return "\n".join(lines)

    @staticmethod
    def _summarize_pending_changes(pending_changes: list[dict[str, Any]]) -> str:
        if not pending_changes:
            return ""
        total_add = 0
        total_del = 0
        for change in pending_changes:
            if not isinstance(change, dict):
                continue
            diff_text = str(change.get("diff") or "")
            additions, deletions = AgentRuntime._count_diff_changes(diff_text)
            total_add += additions
            total_del += deletions
        lines = [
            (
                f"Подготовлены изменения: {len(pending_changes)} "
                f"(+{total_add} -{total_del})."
            )
        ]
        for change in pending_changes[:8]:
            path = str(change.get("path") or "")
            operation = str(change.get("operation") or "")
            diff_text = str(change.get("diff") or "")
            additions, deletions = AgentRuntime._count_diff_changes(diff_text)
            lines.append(f"- {operation}: {path} (+{additions} -{deletions})")
        if len(pending_changes) > 8:
            lines.append(f"... (+{len(pending_changes) - 8} more)")
        return "\n".join(lines)

    @staticmethod
    def _count_diff_changes(diff_text: str) -> tuple[int, int]:
        additions = 0
        deletions = 0
        for line in (diff_text or "").splitlines():
            if line.startswith(("+++", "---", "@@")):
                continue
            if line.startswith("+"):
                additions += 1
                continue
            if line.startswith("-"):
                deletions += 1
        return additions, deletions

    @staticmethod
    def _parse_text_tool_actions(text: str) -> list[dict[str, Any]]:
        raw_text = (text or "").strip()
        if not raw_text:
            return []

        candidates: list[str] = [raw_text]
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw_text, flags=re.DOTALL | re.IGNORECASE)
        candidates.extend(item.strip() for item in fenced if item.strip())

        for candidate in candidates:
            decoded = AgentRuntime._decode_json_candidate(candidate)
            if isinstance(decoded, dict):
                actions = decoded.get("actions")
                if isinstance(actions, list):
                    return [item for item in actions if isinstance(item, dict)]
                if isinstance(decoded.get("action"), dict):
                    return [decoded.get("action")]
                if decoded.get("tool") or decoded.get("name"):
                    return [decoded]
            if isinstance(decoded, list):
                return [item for item in decoded if isinstance(item, dict)]
        return AgentRuntime._parse_function_calls_from_text(raw_text)

    @staticmethod
    def _task_requires_file_changes(message: str) -> bool:
        text = (message or "").lower()
        change_markers = (
            "add test",
            "write test",
            "update test",
            "refactor",
            "fix",
            "implement",
            "change",
            "edit",
            "rewrite",
            "добав",
            "тест",
            "обнов",
            "исправ",
            "рефактор",
            "измени",
            "реализ",
            "доработ",
        )
        return any(marker in text for marker in change_markers)

    @staticmethod
    def _decode_json_candidate(candidate: str) -> Any | None:
        text = candidate.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        first_curly = text.find("{")
        last_curly = text.rfind("}")
        if first_curly >= 0 and last_curly > first_curly:
            try:
                return json.loads(text[first_curly : last_curly + 1])
            except json.JSONDecodeError:
                return None
        return None

    @staticmethod
    def _parse_function_calls_from_text(text: str) -> list[dict[str, Any]]:
        candidates: list[str] = [text]
        candidates.extend(
            item.strip()
            for item in re.findall(r"```(?:[a-zA-Z0-9_+-]+)?\s*(.*?)```", text, flags=re.DOTALL)
            if item.strip()
        )

        parsed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for blob in candidates:
            snippets = [blob.strip()]
            snippets.extend(line.strip() for line in blob.splitlines() if line.strip())
            for snippet in snippets:
                parsed_call = AgentRuntime._parse_python_style_call(snippet)
                if parsed_call is None:
                    continue
                raw_name, args_payload = parsed_call
                normalized_name = _TOOL_NAME_ALIASES.get(raw_name, raw_name)
                if normalized_name not in _SUPPORTED_TOOL_NAMES:
                    continue

                args_payload = {
                    _ARG_NAME_ALIASES.get(key, key): value for key, value in args_payload.items()
                }
                if normalized_name == "write_file" and "content" not in args_payload:
                    args_payload["content"] = ""

                action = {"tool": normalized_name, "args": args_payload}
                marker = json.dumps(action, ensure_ascii=True, sort_keys=True)
                if marker in seen:
                    continue
                seen.add(marker)
                parsed.append(action)

        return parsed

    @staticmethod
    def _parse_python_style_call(snippet: str) -> tuple[str, dict[str, Any]] | None:
        expr = (snippet or "").strip().strip("`")
        if not expr or "(" not in expr or ")" not in expr:
            return None

        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            return None

        body = tree.body
        if not isinstance(body, ast.Call):
            return None
        if not isinstance(body.func, ast.Name):
            return None

        call_name = body.func.id
        args: dict[str, Any] = {}
        for kw in body.keywords:
            if not kw.arg:
                continue
            try:
                args[kw.arg] = ast.literal_eval(kw.value)
            except Exception:  # noqa: BLE001
                try:
                    args[kw.arg] = ast.unparse(kw.value)
                except Exception:  # noqa: BLE001
                    args[kw.arg] = ""
        return call_name, args

    async def delete_chat(self, chat_id: str) -> dict[str, Any]:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")

        await self._ensure_authenticated()
        try:
            raw = await self._client.delete_chat(clean_chat_id)
        except AuthenticationError:
            await self.login()
            raw = await self._client.delete_chat(clean_chat_id)

        self._store.delete_chat(clean_chat_id, autobind=self._config.agent.project_chat_autobind)
        self._agent_memory.pop(clean_chat_id, None)

        return {
            "chat_id": clean_chat_id,
            "deleted": True,
            "raw": raw if isinstance(raw, dict) else {"value": raw},
        }

    async def rename_chat(self, chat_id: str, title: str) -> dict[str, Any]:
        clean_chat_id = (chat_id or "").strip()
        clean_title = (title or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        if not clean_title:
            raise ValueError("title is required")

        await self._ensure_authenticated()

        remote_updated = False
        raw: dict[str, Any] = {}
        try:
            remote_payload = await self._client.update_chat_title(clean_chat_id, clean_title)
            if isinstance(remote_payload, dict):
                raw = remote_payload
            remote_updated = True
        except AuthenticationError:
            await self.login()
            try:
                remote_payload = await self._client.update_chat_title(clean_chat_id, clean_title)
                if isinstance(remote_payload, dict):
                    raw = remote_payload
                remote_updated = True
            except RequestFailedError as exc:
                raw = {"warning": str(exc)}
        except RequestFailedError as exc:
            raw = {"warning": str(exc)}

        self._store.rename_chat_title(clean_chat_id, clean_title)
        return {
            "chat_id": clean_chat_id,
            "title": clean_title,
            "remote_updated": remote_updated,
            "raw": raw,
        }

    async def apply_pending_changes(self, pending_id: str) -> dict[str, Any]:
        clean_pending_id = str(pending_id or "").strip()
        if not clean_pending_id:
            raise ValueError("pending_id is required")

        pending_changes = self._pending_change_sets.pop(clean_pending_id, None)
        if not pending_changes:
            raise ValueError("pending changes not found or already handled")

        workspace = WorkspaceTools(self._store.project_root)
        applied_files: list[str] = []
        errors: list[str] = []
        applied_count = 0
        file_snapshots: dict[str, dict[str, Any]] = {}

        for change in pending_changes:
            if not isinstance(change, dict):
                continue
            operation = str(change.get("operation") or "").strip()
            args_value = change.get("apply_args")
            args = args_value if isinstance(args_value, dict) else {}
            if operation not in {"write_file", "replace_in_file", "delete_file"}:
                continue
            path_hint = str(args.get("path") or change.get("path") or "").strip()
            if path_hint and path_hint not in file_snapshots:
                snapshot = self._snapshot_file_state(path_hint)
                if snapshot is not None:
                    file_snapshots[path_hint] = snapshot
            try:
                result = workspace.execute(operation, args, auto_apply=True)
            except WorkspaceToolError as exc:
                errors.append(f"{operation}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{operation}: {exc}")
                continue

            if isinstance(result, dict) and result.get("changed"):
                applied_count += 1
                path = str(result.get("path") or "").strip()
                if path and path not in applied_files:
                    applied_files.append(path)

        applied_change_id: str | None = None
        if applied_count > 0 and file_snapshots:
            applied_change_id = self._register_applied_change_set(list(file_snapshots.values()))

        return {
            "pending_id": clean_pending_id,
            "applied_count": applied_count,
            "applied_files": applied_files,
            "errors": errors,
            "applied_change_id": applied_change_id,
        }

    async def discard_pending_changes(self, pending_id: str) -> dict[str, Any]:
        clean_pending_id = str(pending_id or "").strip()
        if not clean_pending_id:
            raise ValueError("pending_id is required")
        removed = self._pending_change_sets.pop(clean_pending_id, None)
        return {
            "pending_id": clean_pending_id,
            "discarded": removed is not None,
        }

    async def discard_applied_changes(self, applied_change_id: str) -> dict[str, Any]:
        clean_id = str(applied_change_id or "").strip()
        if not clean_id:
            raise ValueError("applied_change_id is required")
        removed = self._applied_change_sets.pop(clean_id, None)
        return {
            "applied_change_id": clean_id,
            "discarded": removed is not None,
        }

    async def undo_applied_changes(self, applied_change_id: str) -> dict[str, Any]:
        clean_id = str(applied_change_id or "").strip()
        if not clean_id:
            raise ValueError("applied_change_id is required")

        snapshots = self._applied_change_sets.pop(clean_id, None)
        if not snapshots:
            raise ValueError("applied changes not found or already handled")

        undone_files: list[str] = []
        errors: list[str] = []

        for item in snapshots:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue

            existed_before = bool(item.get("existed"))
            before_content = str(item.get("content") or "")
            try:
                target = self._resolve_workspace_path(path)
                if existed_before:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(before_content, encoding="utf-8")
                else:
                    if target.exists():
                        if target.is_file():
                            target.unlink()
                        else:
                            errors.append(f"{path}: cannot remove non-file path")
                            continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {exc}")
                continue

            if path not in undone_files:
                undone_files.append(path)

        return {
            "applied_change_id": clean_id,
            "undone_files": undone_files,
            "undone_count": len(undone_files),
            "errors": errors,
        }

    async def get_chat_history(self, chat_id: str) -> list[dict[str, str]]:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")

        local_history = self._normalize_history_messages(self._store.list_chat_messages(clean_chat_id))
        await self._ensure_authenticated()
        try:
            remote_history = await self._client.get_chat_history(clean_chat_id)
        except AuthenticationError:
            await self.login()
            try:
                remote_history = await self._client.get_chat_history(clean_chat_id)
            except RequestFailedError:
                return local_history
        except RequestFailedError:
            return local_history

        normalized = self._normalize_history_messages(remote_history)
        if normalized:
            self._store.replace_chat_messages(clean_chat_id, normalized)
            return normalized
        return local_history

    async def resolve_model(self, requested_model: str | None) -> str:
        model_id = requested_model or self._config.agent.default_model
        if not model_id:
            raise ValueError("model_id is required (or set agent.default_model in config)")

        models = await self.list_models()
        available = {item.get("id") for item in models if isinstance(item, dict)}
        if model_id not in available:
            raise ModelNotFoundError(f"Model '{model_id}' is not available in OpenWebUI")

        return model_id

    async def _ensure_authenticated(self) -> None:
        authenticated, _ = await self._client.session_check()
        if authenticated:
            return
        await self.login()

    def _resolve_credentials(
        self,
        username: str | None,
        password: str | None,
    ) -> tuple[str, str]:
        resolved_username = username or self._config.openwebui.credentials.username
        resolved_password = password or self._config.openwebui.credentials.password

        if not resolved_username or not resolved_password:
            raise ValueError(
                "Credentials are missing. Provide username/password in request or in config file"
            )

        return resolved_username, resolved_password

    @staticmethod
    def _extract_chat_id(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None

        for key in ("chat_id", "chatId", "id"):
            value = payload.get(key)
            if value:
                return str(value)

        nested = payload.get("chat")
        if isinstance(nested, dict):
            for key in ("chat_id", "chatId", "id"):
                value = nested.get(key)
                if value:
                    return str(value)

        return None

    @staticmethod
    def _extract_assistant_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            return _strip_reasoning_content(str(payload))

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    text = _normalize_content(content)
                    if text:
                        return _strip_reasoning_content(text)

                text = first.get("text")
                if isinstance(text, str) and text.strip():
                    return _strip_reasoning_content(text)

        for key in ("response", "answer", "output", "message", "content"):
            value = payload.get(key)
            text = _normalize_content(value)
            if text:
                return _strip_reasoning_content(text)

        return _strip_reasoning_content(json.dumps(payload, ensure_ascii=True))

    @staticmethod
    def _extract_assistant_turn(payload: Any) -> tuple[str | None, list[dict[str, Any]]]:
        if not isinstance(payload, dict):
            return None, []

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None, []

        first = choices[0]
        if not isinstance(first, dict):
            return None, []

        message = first.get("message")
        if not isinstance(message, dict):
            return None, []

        assistant_text = _strip_reasoning_content(_normalize_content(message.get("content")) or "")
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            normalized_calls = [item for item in tool_calls if isinstance(item, dict)]
            if normalized_calls:
                return assistant_text or None, normalized_calls

        function_call = message.get("function_call")
        if isinstance(function_call, dict):
            return assistant_text or None, [{"type": "function", "function": function_call}]

        return assistant_text, []

    @staticmethod
    def _extract_tool_name(tool_call: dict[str, Any]) -> str:
        function_block = tool_call.get("function")
        if isinstance(function_block, dict):
            name = function_block.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        name = tool_call.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return ""

    @staticmethod
    def _extract_tool_args(tool_call: dict[str, Any]) -> dict[str, Any]:
        function_block = tool_call.get("function")
        raw_args: Any = None
        if isinstance(function_block, dict):
            raw_args = function_block.get("arguments")
        if raw_args is None:
            raw_args = tool_call.get("arguments")

        if raw_args is None:
            return {}
        if isinstance(raw_args, dict):
            return raw_args
        if not isinstance(raw_args, str):
            return {}

        text = raw_args.strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return decoded
        return {}

    @staticmethod
    def _build_tool_message(
        tool_call: dict[str, Any],
        tool_payload: dict[str, Any],
        step_index: int,
    ) -> dict[str, Any]:
        tool_call_id = tool_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            tool_call_id = f"tool_call_{step_index}"
        tool_name = AgentRuntime._extract_tool_name(tool_call) or "tool"
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps(tool_payload, ensure_ascii=True),
        }

    def _execute_tool_call(
        self,
        workspace: WorkspaceTools,
        tool_call: dict[str, Any],
        *,
        auto_apply: bool,
    ) -> dict[str, Any]:
        tool_name = self._extract_tool_name(tool_call)
        if not tool_name:
            return {"ok": False, "error": "tool name is missing"}
        tool_name = _TOOL_NAME_ALIASES.get(tool_name, tool_name)

        tool_args = self._extract_tool_args(tool_call)
        if tool_name == "replace_in_file" and "replace" not in tool_args:
            tool_args = dict(tool_args)
            tool_args["replace"] = ""
        if tool_name == "write_file" and "content" not in tool_args:
            tool_args = dict(tool_args)
            tool_args["content"] = ""
        try:
            result = workspace.execute(tool_name, tool_args, auto_apply=auto_apply)
        except WorkspaceToolError as exc:
            return {"ok": False, "name": tool_name, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "name": tool_name, "error": f"tool execution failed: {exc}"}

        return {"ok": True, "name": tool_name, "result": result}

    @staticmethod
    def _tool_result_stream_event(tool_payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(tool_payload, dict):
            return {"type": "tool_result", "ok": False, "text": "Инструмент завершился с ошибкой"}
        ok = bool(tool_payload.get("ok"))
        name = str(tool_payload.get("name") or "tool").strip() or "tool"
        if not ok:
            error = str(tool_payload.get("error") or "unknown error").strip()
            return {
                "type": "tool_result",
                "ok": False,
                "name": name,
                "error": error,
                "text": f"{name}: ошибка: {error}",
            }

        result = tool_payload.get("result")
        if not isinstance(result, dict):
            return {
                "type": "tool_result",
                "ok": True,
                "name": name,
                "text": f"{name}: выполнено",
            }

        path = str(result.get("path") or "").strip()
        if path:
            summary = f"{name}: {path}"
        else:
            summary = f"{name}: выполнено"

        if result.get("applied") is False:
            summary = f"{name}: подготовлены изменения"
        elif result.get("changed") is False:
            summary = f"{name}: без изменений"
        return {
            "type": "tool_result",
            "ok": True,
            "name": name,
            "path": path,
            "text": summary,
        }

    @staticmethod
    def _emit_stream_event(
        callback: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any] | None,
        *,
        chat_id: str | None,
        model_id: str | None,
        step: int | None = None,
    ) -> None:
        if callback is None:
            return
        if not isinstance(event, dict):
            return
        payload = dict(event)
        if chat_id and "chat_id" not in payload:
            payload["chat_id"] = chat_id
        if model_id and "model_id" not in payload:
            payload["model_id"] = model_id
        if step is not None and "step" not in payload:
            payload["step"] = step
        try:
            callback(payload)
        except Exception:  # noqa: BLE001
            # Stream callback errors must not break agent workflow.
            pass

    @staticmethod
    def _extract_changed_path(tool_payload: dict[str, Any]) -> str | None:
        if not isinstance(tool_payload, dict):
            return None
        if not tool_payload.get("ok"):
            return None
        result = tool_payload.get("result")
        if not isinstance(result, dict):
            return None
        if result.get("applied") is False:
            return None
        if not result.get("changed"):
            return None
        path = result.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        return path.strip()

    @staticmethod
    def _extract_pending_change(tool_payload: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(tool_payload, dict):
            return None
        if not tool_payload.get("ok"):
            return None
        name = str(tool_payload.get("name") or "").strip()
        if name not in {"write_file", "replace_in_file", "delete_file"}:
            return None

        result = tool_payload.get("result")
        if not isinstance(result, dict):
            return None
        if result.get("applied") is not False:
            return None
        if not result.get("changed"):
            return None

        path = str(result.get("path") or "").strip()
        apply_args_value = result.get("apply_args")
        apply_args = apply_args_value if isinstance(apply_args_value, dict) else {}
        diff_text = str(result.get("diff") or "")
        return {
            "operation": str(result.get("operation") or name),
            "path": path,
            "diff": diff_text,
            "apply_args": apply_args,
        }

    def _register_pending_changes(self, pending_changes: list[dict[str, Any]]) -> str | None:
        clean_changes = [item for item in pending_changes if isinstance(item, dict)]
        if not clean_changes:
            return None
        pending_id = str(uuid4())
        # Store copy to avoid external mutations.
        self._pending_change_sets[pending_id] = json.loads(json.dumps(clean_changes, ensure_ascii=True))
        if len(self._pending_change_sets) > 64:
            oldest_key = next(iter(self._pending_change_sets))
            if oldest_key != pending_id:
                self._pending_change_sets.pop(oldest_key, None)
        return pending_id

    def _register_applied_change_set(self, snapshots: list[dict[str, Any]]) -> str:
        applied_change_id = str(uuid4())
        self._applied_change_sets[applied_change_id] = json.loads(json.dumps(snapshots, ensure_ascii=True))
        if len(self._applied_change_sets) > 64:
            oldest_key = next(iter(self._applied_change_sets))
            if oldest_key != applied_change_id:
                self._applied_change_sets.pop(oldest_key, None)
        return applied_change_id

    def _snapshot_file_state(self, path: str) -> dict[str, Any] | None:
        clean_path = str(path or "").strip()
        if not clean_path:
            return None
        target = self._resolve_workspace_path(clean_path)
        existed = target.exists() and target.is_file()
        content = ""
        if existed:
            content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "path": clean_path,
            "existed": existed,
            "content": content,
        }

    def _resolve_workspace_path(self, relative_path: str) -> Path:
        clean = str(relative_path or "").strip()
        if not clean:
            raise ValueError("path is required")
        project_root = self._store.project_root.resolve()
        candidate = (project_root / clean).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError as exc:
            raise ValueError("Path escapes project root") from exc
        return candidate

    @staticmethod
    def _merge_chats(
        remote_chats: list[dict[str, Any]],
        local_chats: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        local_titles: dict[str, str] = {}

        for chat in local_chats:
            if not isinstance(chat, dict):
                continue
            chat_id = chat.get("chat_id")
            if not chat_id:
                continue
            title = str(chat.get("title") or "").strip()
            if title:
                local_titles[str(chat_id)] = title

        for source in (local_chats, remote_chats):
            for chat in source:
                if not isinstance(chat, dict):
                    continue
                chat_id = chat.get("chat_id")
                if not chat_id:
                    continue

                key = str(chat_id)
                existing = by_id.get(key, {})
                merged = dict(existing)
                merged.update({k: v for k, v in chat.items() if v is not None})
                merged["chat_id"] = key
                by_id[key] = merged

        chats = list(by_id.values())
        for item in chats:
            chat_id = str(item.get("chat_id") or "").strip()
            local_title = local_titles.get(chat_id)
            if local_title:
                item["title"] = local_title

        def sort_key(item: dict[str, Any]) -> tuple[int, str]:
            stamp = item.get("updated_at") or item.get("created_at") or ""
            if not isinstance(stamp, str):
                stamp = str(stamp)
            return (1 if stamp else 0, stamp)

        chats.sort(key=sort_key, reverse=True)
        return chats

    @staticmethod
    def _normalize_history_messages(
        history: Any,
        *,
        limit: int | None = None,
    ) -> list[dict[str, str]]:
        if not isinstance(history, list):
            return []

        items = history
        if isinstance(limit, int) and limit > 0:
            items = history[-limit:]

        normalized: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            if role == "assistant":
                content = _strip_reasoning_content(content)
                if not content:
                    continue
            stamp = str(item.get("created_at") or "").strip()
            payload: dict[str, str] = {"role": role, "content": content}
            if stamp:
                payload["created_at"] = stamp
            normalized.append(payload)
        return normalized


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_content(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        cleaned = _strip_reasoning_content(cleaned)
        return cleaned or None

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                cleaned = _strip_reasoning_content(item)
                if cleaned:
                    parts.append(cleaned)
                continue

            if isinstance(item, dict):
                raw_type = str(item.get("type") or "").strip().lower()
                if raw_type in {"reasoning", "thinking", "analysis", "thought"}:
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    cleaned = _strip_reasoning_content(text)
                    if cleaned:
                        parts.append(cleaned)

        if parts:
            return "\n".join(parts)

    if isinstance(value, dict):
        raw_type = str(value.get("type") or "").strip().lower()
        if raw_type in {"reasoning", "thinking", "analysis", "thought"}:
            return None
        text = value.get("content") or value.get("text")
        if isinstance(text, str) and text.strip():
            cleaned = _strip_reasoning_content(text)
            return cleaned or None

    return None


def _strip_reasoning_content(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    for pattern in _REASONING_TAG_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = _REASONING_FENCE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
