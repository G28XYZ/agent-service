from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_service.config import AppConfig
from agent_service.service import AgentRuntime
from agent_service.session_store import SessionStore


class _NoToolClient:
    async def chat_completion_stream(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        chat_id: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_event: Any = None,
    ) -> dict[str, Any]:
        del model_id, messages, chat_id, tools, on_event
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Не смог вызвать инструменты напрямую.",
                    }
                }
            ]
        }


class _BridgeAgentRuntime(AgentRuntime):
    async def resolve_model(self, requested_model: str | None) -> str:
        return requested_model or "model-a"

    async def _ensure_authenticated(self) -> None:
        return

    async def _ensure_chat_for_task(
        self,
        selected_model: str,
        requested_chat_id: str | None,
        initial_message: str,
    ) -> tuple[str | None, str | None]:
        del selected_model, initial_message
        return requested_chat_id or "chat-1", "Bridge Test Chat"

    async def _load_chat_history_context(self, chat_id: str | None) -> list[dict[str, str]]:
        del chat_id
        return []

    async def _fallback_agent_to_message(
        self,
        *,
        selected_model: str,
        clean_message: str,
        chat_id: str | None,
        reason: str,
        workspace: Any,
        auto_apply: bool,
        tool_policy: Any,
        history_messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        del (
            selected_model,
            clean_message,
            reason,
            workspace,
            auto_apply,
            tool_policy,
            history_messages,
        )
        return {
            "chat_id": chat_id or "chat-1",
            "model_id": "model-a",
            "assistant_message": "```path: src/generated.py\nprint('bridge')\n```",
            "raw": {"fallback": True},
            "applied_files": [],
            "pending_id": None,
            "pending_changes": [],
            "tool_steps": 0,
        }


class AgentRuntimeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_mode_bridges_code_blocks_without_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            store = SessionStore(project_root)
            config = AppConfig.model_validate(
                {
                    "openwebui": {
                        "base_url": "http://openwebui.local",
                        "verify_tls": False,
                    },
                    "agent": {
                        "default_model": "model-a",
                        "project_chat_autobind": True,
                    },
                    "http": {
                        "timeout_seconds": 5,
                        "retries": 0,
                        "user_agent": "AgentRuntimeBridgeTest/0.1",
                        "use_env_proxy": False,
                    },
                }
            )
            runtime = _BridgeAgentRuntime(config, store, _NoToolClient())  # type: ignore[arg-type]

            result = await runtime.run_agent_task(
                message="Создай файл src/generated.py с print('bridge')",
                model_id="model-a",
                chat_id="chat-1",
                auto_apply=False,
            )

            pending_changes = result.get("pending_changes")
            self.assertIsInstance(pending_changes, list)
            self.assertEqual(len(pending_changes), 1)
            pending_item = pending_changes[0]
            self.assertEqual(pending_item.get("operation"), "write_file")
            self.assertEqual(pending_item.get("path"), "src/generated.py")
            self.assertIn("Подготовлены изменения", str(result.get("assistant_message") or ""))
            self.assertFalse((project_root / "src" / "generated.py").exists())


if __name__ == "__main__":
    unittest.main()
