from __future__ import annotations

import difflib
import fnmatch
import os
from pathlib import Path
from typing import Any


class WorkspaceToolError(RuntimeError):
    pass


class WorkspaceTools:
    _IGNORED_DIR_NAMES = {
        ".agent-service",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }

    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()

    @staticmethod
    def tool_definitions() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files in the project.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative directory path"},
                            "glob": {
                                "type": "string",
                                "description": "Optional glob filter, e.g. '*.py' or '**/*.md'",
                            },
                            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a UTF-8 text file from the project.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path"},
                            "max_chars": {"type": "integer", "minimum": 1, "maximum": 500000},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_in_files",
                    "description": "Search a text snippet in project files.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search text"},
                            "path": {"type": "string", "description": "Relative directory path"},
                            "glob": {
                                "type": "string",
                                "description": "Optional glob filter, e.g. '*.py' or '**/*.md'",
                            },
                            "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                            "ignore_case": {"type": "boolean"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": (
                        "Create file contents. For existing non-empty files, "
                        "full overwrite requires allow_overwrite=true."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path"},
                            "content": {"type": "string", "description": "New file content"},
                            "allow_overwrite": {
                                "type": "boolean",
                                "description": (
                                    "Explicitly allow replacing an existing non-empty file. "
                                    "Use replace_in_file for targeted edits."
                                ),
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "replace_in_file",
                    "description": "Replace text in a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path"},
                            "find": {"type": "string", "description": "Exact text to find"},
                            "replace": {"type": "string", "description": "Replacement text"},
                            "count": {
                                "type": "integer",
                                "minimum": 0,
                                "description": "0 means replace all occurrences",
                            },
                        },
                        "required": ["path", "find", "replace"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": "Delete a file from the project.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path"},
                        },
                        "required": ["path"],
                    },
                },
            },
        ]

    def execute(self, name: str, args: dict[str, Any], *, auto_apply: bool) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise WorkspaceToolError("tool arguments must be a JSON object")

        if name == "list_files":
            path = _as_str(args.get("path"), default=".")
            glob_value = _optional_str(args.get("glob"))
            limit = _as_int(args.get("limit"), default=200, minimum=1, maximum=1000)
            return {"files": self.list_files(path=path, glob_value=glob_value, limit=limit)}

        if name == "read_file":
            path = _required_str(args.get("path"), field_name="path")
            max_chars = _as_int(args.get("max_chars"), default=50000, minimum=1, maximum=500000)
            return self.read_file(path=path, max_chars=max_chars)

        if name == "search_in_files":
            query = _required_str(args.get("query"), field_name="query")
            path = _as_str(args.get("path"), default=".")
            glob_value = _optional_str(args.get("glob"))
            limit = _as_int(args.get("limit"), default=100, minimum=1, maximum=5000)
            ignore_case = bool(args.get("ignore_case", True))
            return self.search_in_files(
                query=query,
                path=path,
                glob_value=glob_value,
                limit=limit,
                ignore_case=ignore_case,
            )

        if name == "write_file":
            path = _required_str(args.get("path"), field_name="path")
            content = _required_str(args.get("content"), field_name="content")
            allow_overwrite = bool(args.get("allow_overwrite", False))
            if not auto_apply:
                return self.preview_write_file(path=path, content=content, allow_overwrite=allow_overwrite)
            return self.write_file(path=path, content=content, allow_overwrite=allow_overwrite)

        if name == "replace_in_file":
            path = _required_str(args.get("path"), field_name="path")
            find = _required_str(args.get("find"), field_name="find")
            replace = _required_replace(args)
            count = _as_int(args.get("count"), default=0, minimum=0, maximum=1000000)
            if not auto_apply:
                return self.preview_replace_in_file(path=path, find=find, replace=replace, count=count)
            return self.replace_in_file(path=path, find=find, replace=replace, count=count)

        if name == "delete_file":
            path = _required_str(args.get("path"), field_name="path")
            if not auto_apply:
                return self.preview_delete_file(path=path)
            return self.delete_file(path=path)

        raise WorkspaceToolError(f"Unknown tool: {name}")

    def list_files(self, *, path: str, glob_value: str | None, limit: int) -> list[str]:
        base_path = self._resolve_path(path)
        if not base_path.exists():
            raise WorkspaceToolError(f"Path does not exist: {path}")

        if base_path.is_file():
            relative = base_path.relative_to(self.project_root).as_posix()
            if glob_value and not self._matches_glob(relative, base_path.name, glob_value):
                return []
            return [relative]

        results: list[str] = []
        for file_path in self._iter_files(base_path):
            relative = file_path.relative_to(self.project_root).as_posix()
            if glob_value and not self._matches_glob(relative, file_path.name, glob_value):
                continue
            results.append(relative)
            if len(results) >= limit:
                break

        return sorted(results)

    def read_file(self, *, path: str, max_chars: int) -> dict[str, Any]:
        file_path = self._resolve_path(path)
        if not file_path.exists() or not file_path.is_file():
            raise WorkspaceToolError(f"File not found: {path}")

        text = file_path.read_text(encoding="utf-8", errors="replace")
        total_chars = len(text)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        return {
            "path": file_path.relative_to(self.project_root).as_posix(),
            "content": text,
            "truncated": truncated,
            "total_chars": total_chars,
        }

    def search_in_files(
        self,
        *,
        query: str,
        path: str,
        glob_value: str | None,
        limit: int,
        ignore_case: bool,
    ) -> dict[str, Any]:
        base_path = self._resolve_path(path)
        if not base_path.exists():
            raise WorkspaceToolError(f"Path does not exist: {path}")

        query_value = query.lower() if ignore_case else query
        matches: list[dict[str, Any]] = []
        files_scanned = 0

        for file_path in self._iter_files(base_path):
            relative = file_path.relative_to(self.project_root).as_posix()
            if glob_value and not self._matches_glob(relative, file_path.name, glob_value):
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            files_scanned += 1
            for line_no, raw_line in enumerate(content.splitlines(), start=1):
                haystack = raw_line.lower() if ignore_case else raw_line
                if query_value in haystack:
                    matches.append(
                        {
                            "path": relative,
                            "line": line_no,
                            "text": raw_line[:500],
                        }
                    )
                    if len(matches) >= limit:
                        return {"matches": matches, "files_scanned": files_scanned, "truncated": True}

        return {"matches": matches, "files_scanned": files_scanned, "truncated": False}

    def write_file(self, *, path: str, content: str, allow_overwrite: bool = False) -> dict[str, Any]:
        file_path = self._resolve_path(path)
        existed = file_path.exists()
        if existed and not file_path.is_file():
            raise WorkspaceToolError(f"Path is not a file: {path}")
        if existed:
            before = file_path.read_text(encoding="utf-8", errors="replace")
            if before and before != content and not allow_overwrite:
                raise WorkspaceToolError(
                    "Refusing full overwrite of existing non-empty file. "
                    "Use replace_in_file for targeted edits or set allow_overwrite=true."
                )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        relative = file_path.relative_to(self.project_root).as_posix()
        return {
            "path": relative,
            "changed": True,
            "applied": True,
            "operation": "write_file",
            "created": not existed,
            "bytes": len(content.encode("utf-8")),
        }

    def replace_in_file(self, *, path: str, find: str, replace: str, count: int) -> dict[str, Any]:
        if not find:
            raise WorkspaceToolError("find must not be empty")

        file_path = self._resolve_path(path)
        if not file_path.exists() or not file_path.is_file():
            raise WorkspaceToolError(f"File not found: {path}")

        original = file_path.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(find)
        if occurrences == 0:
            return {
                "path": file_path.relative_to(self.project_root).as_posix(),
                "changed": False,
                "occurrences": 0,
                "replaced": 0,
            }

        if count == 0:
            updated = original.replace(find, replace)
            replaced = occurrences
        else:
            updated = original.replace(find, replace, count)
            replaced = min(occurrences, count)

        file_path.write_text(updated, encoding="utf-8")
        relative = file_path.relative_to(self.project_root).as_posix()
        return {
            "path": relative,
            "changed": True,
            "applied": True,
            "operation": "replace_in_file",
            "occurrences": occurrences,
            "replaced": replaced,
        }

    def delete_file(self, *, path: str) -> dict[str, Any]:
        file_path = self._resolve_path(path)
        if not file_path.exists() or not file_path.is_file():
            raise WorkspaceToolError(f"File not found: {path}")

        relative = file_path.relative_to(self.project_root).as_posix()
        file_path.unlink()
        return {
            "path": relative,
            "changed": True,
            "applied": True,
            "operation": "delete_file",
            "deleted": True,
        }

    def preview_write_file(self, *, path: str, content: str, allow_overwrite: bool = False) -> dict[str, Any]:
        file_path = self._resolve_path(path)
        existed = file_path.exists()
        if existed and not file_path.is_file():
            raise WorkspaceToolError(f"Path is not a file: {path}")

        before = ""
        if existed:
            before = file_path.read_text(encoding="utf-8", errors="replace")
            if before and before != content and not allow_overwrite:
                raise WorkspaceToolError(
                    "Refusing full overwrite of existing non-empty file. "
                    "Use replace_in_file for targeted edits or set allow_overwrite=true."
                )
        after = content
        changed = before != after
        relative = file_path.relative_to(self.project_root).as_posix()

        return {
            "path": relative,
            "changed": changed,
            "applied": False,
            "operation": "write_file",
            "created": not existed,
            "bytes": len(content.encode("utf-8")),
            "diff": _build_unified_diff(before, after, relative, before_exists=existed, after_exists=True),
            "apply_args": {
                "path": relative,
                "content": content,
                "allow_overwrite": allow_overwrite,
            },
        }

    def preview_replace_in_file(self, *, path: str, find: str, replace: str, count: int) -> dict[str, Any]:
        if not find:
            raise WorkspaceToolError("find must not be empty")

        file_path = self._resolve_path(path)
        if not file_path.exists() or not file_path.is_file():
            raise WorkspaceToolError(f"File not found: {path}")

        original = file_path.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(find)
        if occurrences == 0:
            updated = original
            replaced = 0
            changed = False
        elif count == 0:
            updated = original.replace(find, replace)
            replaced = occurrences
            changed = updated != original
        else:
            updated = original.replace(find, replace, count)
            replaced = min(occurrences, count)
            changed = updated != original

        relative = file_path.relative_to(self.project_root).as_posix()
        return {
            "path": relative,
            "changed": changed,
            "applied": False,
            "operation": "replace_in_file",
            "occurrences": occurrences,
            "replaced": replaced,
            "diff": _build_unified_diff(
                original,
                updated,
                relative,
                before_exists=True,
                after_exists=True,
            ),
            "apply_args": {
                "path": relative,
                "find": find,
                "replace": replace,
                "count": count,
            },
        }

    def preview_delete_file(self, *, path: str) -> dict[str, Any]:
        file_path = self._resolve_path(path)
        if not file_path.exists() or not file_path.is_file():
            raise WorkspaceToolError(f"File not found: {path}")

        before = file_path.read_text(encoding="utf-8", errors="replace")
        relative = file_path.relative_to(self.project_root).as_posix()
        return {
            "path": relative,
            "changed": True,
            "applied": False,
            "operation": "delete_file",
            "deleted": True,
            "diff": _build_unified_diff(before, "", relative, before_exists=True, after_exists=False),
            "apply_args": {
                "path": relative,
            },
        }

    def _iter_files(self, base_path: Path):
        if base_path.is_file():
            yield base_path
            return

        for root, dirs, files in os.walk(base_path):
            dirs[:] = [name for name in dirs if name not in self._IGNORED_DIR_NAMES]
            root_path = Path(root)
            for filename in files:
                if filename.startswith(".DS_Store"):
                    continue
                yield root_path / filename

    def _resolve_path(self, raw_path: str) -> Path:
        clean = (raw_path or "").strip() or "."
        candidate = (self.project_root / clean).resolve()
        project_root_str = str(self.project_root)
        candidate_str = str(candidate)
        if os.path.commonpath([project_root_str, candidate_str]) != project_root_str:
            raise WorkspaceToolError("Path escapes project root")
        return candidate

    @staticmethod
    def _matches_glob(relative: str, filename: str, glob_value: str) -> bool:
        pattern = glob_value.strip()
        if not pattern:
            return True
        if fnmatch.fnmatch(relative, pattern):
            return True
        return fnmatch.fnmatch(filename, pattern)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_str(value: Any, *, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _required_str(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkspaceToolError(f"{field_name} is required")
    return text


def _required_replace(args: dict[str, Any]) -> str:
    if "replace" not in args:
        raise WorkspaceToolError("replace is required")
    value = args.get("replace")
    if value is None:
        raise WorkspaceToolError("replace is required")
    return str(value)


def _as_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None or value == "":
        return default
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkspaceToolError("Expected integer argument") from exc
    if resolved < minimum:
        return minimum
    if resolved > maximum:
        return maximum
    return resolved


def _build_unified_diff(
    before: str,
    after: str,
    relative_path: str,
    *,
    before_exists: bool,
    after_exists: bool,
) -> str:
    if before == after:
        return ""

    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)

    from_name = f"a/{relative_path}" if before_exists else "/dev/null"
    to_name = f"b/{relative_path}" if after_exists else "/dev/null"

    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=from_name,
        tofile=to_name,
        lineterm="",
    )
    return "\n".join(diff)
