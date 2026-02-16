from __future__ import annotations

import asyncio
import unittest
from typing import Any

from agent_service.protocol_runtime import AgentProtocolRuntime


class FakeAgentRuntime:
    async def run_agent_task(
        self,
        message: str,
        model_id: str | None = None,
        chat_id: str | None = None,
        *,
        auto_apply: bool = True,
        stream_callback: Any = None,
    ) -> dict[str, Any]:
        if stream_callback is not None:
            stream_callback({"type": "status", "text": "Шаг 1: запрос к модели"})
            stream_callback({"type": "tool_start", "name": "read_file"})
            stream_callback({"type": "tool_result", "name": "read_file", "ok": True})
            stream_callback({"type": "assistant_delta", "text": "Готово"})
        return {
            "chat_id": chat_id or "chat-1",
            "model_id": model_id or "model-x",
            "assistant_message": f"done: {message}",
            "applied_files": ["README.md"] if auto_apply else [],
            "pending_changes": [],
            "tool_steps": 1,
            "chat_title": "Task",
            "raw": {"ignored": True},
        }


class SlowFakeAgentRuntime:
    async def run_agent_task(
        self,
        message: str,
        model_id: str | None = None,
        chat_id: str | None = None,
        *,
        auto_apply: bool = True,
        stream_callback: Any = None,
    ) -> dict[str, Any]:
        if stream_callback is not None:
            stream_callback({"type": "status", "text": "waiting"})
        await asyncio.sleep(10)
        return {
            "chat_id": chat_id or "chat-slow",
            "model_id": model_id or "model-slow",
            "assistant_message": message,
            "applied_files": [],
            "pending_changes": [],
            "tool_steps": 0,
        }


class ProtocolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_run_emits_cycle_phases(self) -> None:
        runtime = AgentProtocolRuntime(FakeAgentRuntime())  # type: ignore[arg-type]
        session = runtime.create_session(model_id="model-x")
        events: list[dict[str, Any]] = []

        run_id = await runtime.start_prompt(
            session_id=session["session_id"],
            message="прочитай README.md",
            auto_apply=True,
            on_event=lambda payload: events.append(payload),
        )
        run = await runtime.wait_run(run_id, timeout_seconds=1)

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["result"]["assistant_message"], "done: прочитай README.md")
        refreshed_session = runtime.get_session(session["session_id"])
        self.assertEqual(refreshed_session["chat_id"], "chat-1")

        phases = {event.get("phase") for event in events}
        self.assertIn("plan", phases)
        self.assertIn("act", phases)
        self.assertIn("verify", phases)
        self.assertIn("final", phases)

    async def test_cancel_active_run(self) -> None:
        runtime = AgentProtocolRuntime(SlowFakeAgentRuntime())  # type: ignore[arg-type]
        session = runtime.create_session(model_id="model-slow")

        run_id = await runtime.start_prompt(
            session_id=session["session_id"],
            message="long task",
            on_event=None,
        )
        await asyncio.sleep(0.05)
        cancel_result = runtime.cancel_run(session_id=session["session_id"], run_id=run_id)
        self.assertTrue(cancel_result["cancelled"])

        run = await runtime.wait_run(run_id, timeout_seconds=1)
        self.assertEqual(run["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
