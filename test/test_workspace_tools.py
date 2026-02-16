from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_service.workspace_tools import WorkspaceToolError, WorkspaceTools


class WorkspaceToolsMoveFileTests(unittest.TestCase):
    def test_preview_move_file_returns_pending_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "src" / "old.txt"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("hello\n", encoding="utf-8")

            tools = WorkspaceTools(root)
            result = tools.execute(
                "move_file",
                {
                    "source_path": "src/old.txt",
                    "destination_path": "src/new.txt",
                },
                auto_apply=False,
            )

            self.assertTrue(result.get("changed"))
            self.assertFalse(result.get("applied"))
            self.assertEqual(result.get("operation"), "move_file")
            self.assertEqual(result.get("source_path"), "src/old.txt")
            self.assertEqual(result.get("destination_path"), "src/new.txt")
            self.assertIn("rename from src/old.txt", str(result.get("diff") or ""))
            self.assertTrue(source.exists())
            self.assertFalse((root / "src" / "new.txt").exists())

    def test_move_file_applies_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "a.txt"
            source.write_text("payload", encoding="utf-8")

            tools = WorkspaceTools(root)
            result = tools.execute(
                "move_file",
                {
                    "source_path": "a.txt",
                    "destination_path": "nested/b.txt",
                },
                auto_apply=True,
            )

            self.assertTrue(result.get("changed"))
            self.assertTrue(result.get("applied"))
            self.assertEqual(result.get("path"), "nested/b.txt")
            self.assertFalse((root / "a.txt").exists())
            self.assertEqual((root / "nested" / "b.txt").read_text(encoding="utf-8"), "payload")

    def test_move_file_overwrite_requires_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "src.txt").write_text("new-data", encoding="utf-8")
            (root / "dst.txt").write_text("old-data", encoding="utf-8")

            tools = WorkspaceTools(root)
            with self.assertRaises(WorkspaceToolError):
                tools.execute(
                    "move_file",
                    {
                        "source_path": "src.txt",
                        "destination_path": "dst.txt",
                    },
                    auto_apply=True,
                )

            result = tools.execute(
                "move_file",
                {
                    "source_path": "src.txt",
                    "destination_path": "dst.txt",
                    "allow_overwrite": True,
                },
                auto_apply=True,
            )

            self.assertTrue(result.get("overwritten"))
            self.assertFalse((root / "src.txt").exists())
            self.assertEqual((root / "dst.txt").read_text(encoding="utf-8"), "new-data")


if __name__ == "__main__":
    unittest.main()
