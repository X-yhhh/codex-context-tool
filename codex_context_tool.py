#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


TOOL_NAME = "codex-context-tool"
DEFAULT_MODEL_SLUG = "gpt-5.5"
MAX_CONTEXT_WINDOW = 1_000_000
DEFAULT_CONTEXT_WINDOW = MAX_CONTEXT_WINDOW
DEFAULT_AUTO_COMPACT_RATIO = 0.9

CONTEXT_KEY = "model_context_window"
COMPACT_KEY = "model_auto_compact_token_limit"
CATALOG_KEY = "model_catalog_json"
OWNED_KEYS = (COMPACT_KEY, CONTEXT_KEY, CATALOG_KEY)


@dataclass(frozen=True)
class RuntimeOptions:
    config_path: Path
    catalog_path: Path
    state_dir: Path
    codex_bin: str | None
    model_slug: str
    context_window: int
    auto_compact_token_limit: int

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def history_path(self) -> Path:
        return self.state_dir / "history.jsonl"

    @property
    def change_record_path(self) -> Path:
        return self.state_dir / "CHANGE_RECORD.md"


@dataclass
class ContextSnapshot:
    model: str | None
    context_window: str | None
    auto_compact_limit: str | None
    model_catalog_json: str | None
    catalog_context_window: int | None
    catalog_max_context_window: int | None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def optional_env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def positive_cli_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def context_window_cli_int(value: str) -> int:
    parsed = positive_cli_int(value)
    if parsed > MAX_CONTEXT_WINDOW:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_CONTEXT_WINDOW}")
    return parsed


def root_int(root: dict[str, str], key: str) -> int | None:
    value = root.get(key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def default_auto_compact_token_limit(context_window: int) -> int:
    return max(1, int(context_window * DEFAULT_AUTO_COMPACT_RATIO))


def validate_context_window(value: int, source: str) -> int:
    if value <= 0:
        raise ValueError(f"{source} must be greater than 0")
    if value > MAX_CONTEXT_WINDOW:
        raise ValueError(f"{source} must be <= {MAX_CONTEXT_WINDOW}")
    return value


def validate_auto_compact_limit(value: int, context_window: int, source: str) -> int:
    if value <= 0:
        raise ValueError(f"{source} must be greater than 0")
    if value > context_window:
        raise ValueError(f"{source} must be <= selected context window ({context_window})")
    return value


def first_defined(*values: int | None) -> int:
    for value in values:
        if value is not None:
            return value
    raise ValueError("expected at least one fallback value")


def expand_path(path: str | Path) -> Path:
    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def slug_to_filename(slug: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug).strip(".-")
    return safe or "model"


def default_config_path() -> Path:
    configured = os.environ.get("CODEX_CONTEXT_CONFIG")
    if configured:
        return expand_path(configured)
    return Path.home() / ".codex" / "config.toml"


def default_state_dir() -> Path:
    configured = os.environ.get("CODEX_CONTEXT_STATE_DIR")
    if configured:
        return expand_path(configured)
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return expand_path(xdg_state) / TOOL_NAME
    return Path.home() / ".local" / "state" / TOOL_NAME


def default_catalog_path(model_slug: str) -> Path:
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = expand_path(xdg_config) if xdg_config else Path.home() / ".config"
    return base / TOOL_NAME / "catalog" / f"{slug_to_filename(model_slug)}-model-catalog.json"


def default_codex_bin() -> str | None:
    configured = os.environ.get("CODEX_BIN")
    if configured:
        return str(expand_path(configured))
    resolved = shutil.which("codex")
    if resolved:
        return resolved
    mac_app = Path("/Applications/Codex.app/Contents/Resources/codex")
    if mac_app.exists():
        return str(mac_app)
    return None


def ensure_state_dirs(runtime: RuntimeOptions) -> None:
    runtime.backup_dir.mkdir(parents=True, exist_ok=True)
    runtime.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    runtime.state_dir.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def split_keepends(text: str) -> list[str]:
    return [] if text == "" else text.splitlines(keepends=True)


def line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"


def table_start_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("#"):
            return index
    return len(lines)


def unquote_toml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip('"')
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    return value


def strip_toml_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if in_double:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_double = False
            continue
        if in_single:
            if char == "'":
                in_single = False
            continue
        if char == '"':
            in_double = True
            continue
        if char == "'":
            in_single = True
            continue
        if char == "#":
            return value[:index]
    return value


def root_value_map(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = split_keepends(text)
    for line in lines[: table_start_index(lines)]:
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$", line.rstrip("\r\n"))
        if match:
            values[match.group(1)] = unquote_toml_scalar(strip_toml_comment(match.group(2)))
    return values


def read_catalog_values(catalog_path: Path, model_slug: str) -> tuple[int | None, int | None]:
    if not catalog_path.exists():
        return None, None
    try:
        catalog = json.loads(read_text(catalog_path))
    except (OSError, json.JSONDecodeError):
        return None, None
    model = find_model(catalog, model_slug)
    if not model:
        return None, None
    return model.get("context_window"), model.get("max_context_window")


def snapshot_config(text: str, model_slug: str) -> ContextSnapshot:
    root = root_value_map(text)
    catalog_path = root.get(CATALOG_KEY)
    catalog_context = None
    catalog_max = None
    if catalog_path:
        catalog_context, catalog_max = read_catalog_values(Path(catalog_path).expanduser(), model_slug)
    return ContextSnapshot(
        model=root.get("model"),
        context_window=root.get(CONTEXT_KEY),
        auto_compact_limit=root.get(COMPACT_KEY),
        model_catalog_json=catalog_path,
        catalog_context_window=catalog_context,
        catalog_max_context_window=catalog_max,
    )


def validate_toml_if_possible(text: str) -> str:
    try:
        import tomllib  # type: ignore
    except Exception:
        return "skipped: python tomllib is unavailable"
    tomllib.loads(text)
    return "passed: tomllib parsed config"


def set_root_values(text: str, updates: dict[str, str]) -> tuple[str, list[str]]:
    lines = split_keepends(text)
    first_table = table_start_index(lines)
    changed: list[str] = []
    seen: set[str] = set()
    output: list[str] = []
    root_key_re = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)(.*?)(\r?\n)?$")

    for index, line in enumerate(lines):
        if index >= first_table:
            output.append(line)
            continue
        match = root_key_re.match(line)
        if not match:
            output.append(line)
            continue
        key = match.group(2)
        if key not in updates:
            output.append(line)
            continue
        if key in seen:
            changed.append(f"removed duplicate root {key}")
            continue
        replacement = f"{key} = {updates[key]}{line_ending(line)}"
        if replacement != line:
            changed.append(f"set root {key}: {match.group(4).strip()} -> {updates[key]}")
        output.append(replacement)
        seen.add(key)

    missing = [key for key in updates if key not in seen]
    if missing:
        insert_at = table_start_index(output)
        while insert_at > 0 and output[insert_at - 1].strip() == "":
            insert_at -= 1
        if insert_at > 0 and not output[insert_at - 1].endswith(("\n", "\r\n")):
            output[insert_at - 1] = f"{output[insert_at - 1]}\n"
        insert_lines = [f"{key} = {updates[key]}\n" for key in missing]
        for key in missing:
            changed.append(f"added root {key}: {updates[key]}")
        output[insert_at:insert_at] = insert_lines

    return "".join(output), changed


def remove_root_keys(text: str, keys: Iterable[str]) -> tuple[str, list[str]]:
    key_set = set(keys)
    lines = split_keepends(text)
    first_table = table_start_index(lines)
    changed: list[str] = []
    output: list[str] = []
    root_key_re = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")

    for index, line in enumerate(lines):
        if index < first_table:
            match = root_key_re.match(line)
            if match and match.group(1) in key_set:
                changed.append(f"removed root {match.group(1)}")
                continue
        output.append(line)
    return "".join(output), changed


def make_backup(config_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    candidate = backup_dir / f"config.toml.{stamp}.bak"
    suffix = 1
    while candidate.exists():
        candidate = backup_dir / f"config.toml.{stamp}.{suffix}.bak"
        suffix += 1
    shutil.copy2(config_path, candidate)
    return candidate


def diff_text(before: str, after: str, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def run_codex_debug_models(runtime: RuntimeOptions, args: list[str] | None = None) -> dict:
    if not runtime.codex_bin:
        raise RuntimeError("Codex CLI not found. Set --codex-bin, CODEX_BIN, or pass --source-catalog.")
    command = [runtime.codex_bin, "debug", "models"]
    if args:
        command.extend(args)
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def find_model(catalog: dict, model_slug: str) -> dict | None:
    models = catalog.get("models")
    if not isinstance(models, list):
        return None
    for model in models:
        if isinstance(model, dict) and model.get("slug") == model_slug:
            return model
    return None


def load_source_catalog(runtime: RuntimeOptions, source_catalog: str | None) -> dict:
    if source_catalog:
        return json.loads(read_text(expand_path(source_catalog)))
    if runtime.catalog_path.exists():
        return json.loads(read_text(runtime.catalog_path))
    return run_codex_debug_models(runtime, ["--bundled"])


def patched_catalog_text(runtime: RuntimeOptions, source_catalog: str | None) -> tuple[str, list[str]]:
    catalog = load_source_catalog(runtime, source_catalog)
    model = find_model(catalog, runtime.model_slug)
    if model is None:
        raise RuntimeError(f"{runtime.model_slug} not found in source model catalog")
    before_context = model.get("context_window")
    before_max = model.get("max_context_window")
    model["context_window"] = runtime.context_window
    model["max_context_window"] = runtime.context_window
    notes = [
        f"patched {runtime.model_slug} catalog context_window: {before_context} -> {runtime.context_window}",
        f"patched {runtime.model_slug} catalog max_context_window: {before_max} -> {runtime.context_window}",
    ]
    return json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", notes


def catalog_changed(catalog_path: Path, new_catalog_text: str) -> bool:
    if not catalog_path.exists():
        return True
    try:
        return read_text(catalog_path) != new_catalog_text
    except OSError:
        return True


def record_change(
    runtime: RuntimeOptions,
    command: str,
    changed_files: list[str],
    backup_path: Path | None,
    before: ContextSnapshot,
    after: ContextSnapshot,
    validation: str,
    notes: list[str],
) -> None:
    timestamp = now_iso()
    history = {
        "timestamp": timestamp,
        "command": command,
        "changed_files": changed_files,
        "backup_path": str(backup_path) if backup_path else None,
        "config_path": str(runtime.config_path),
        "catalog_path": str(runtime.catalog_path),
        "model_slug": runtime.model_slug,
        "context_window": runtime.context_window,
        "auto_compact_token_limit": runtime.auto_compact_token_limit,
        "before": before.__dict__,
        "after": after.__dict__,
        "validation": validation,
        "notes": notes,
    }
    runtime.history_path.parent.mkdir(parents=True, exist_ok=True)
    with runtime.history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history, ensure_ascii=False, sort_keys=True) + "\n")

    with runtime.change_record_path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write(f"## {timestamp} - `{command}`\n\n")
        handle.write("Changed files:\n")
        for item in changed_files:
            handle.write(f"- `{item}`\n")
        if backup_path:
            handle.write(f"\nBackup: `{backup_path}`\n")
        handle.write("\nBefore/after context values:\n")
        handle.write(f"- `{COMPACT_KEY}`: `{before.auto_compact_limit}` -> `{after.auto_compact_limit}`\n")
        handle.write(f"- `{CONTEXT_KEY}`: `{before.context_window}` -> `{after.context_window}`\n")
        handle.write(f"- `{CATALOG_KEY}`: `{before.model_catalog_json}` -> `{after.model_catalog_json}`\n")
        handle.write(
            f"- catalog `{runtime.model_slug}.context_window`: "
            f"`{before.catalog_context_window}` -> `{after.catalog_context_window}`\n"
        )
        handle.write(
            f"- catalog `{runtime.model_slug}.max_context_window`: "
            f"`{before.catalog_max_context_window}` -> `{after.catalog_max_context_window}`\n"
        )
        handle.write("\nValidation:\n")
        handle.write(f"- {validation}\n")
        if notes:
            handle.write("\nNotes:\n")
            for note in notes:
                handle.write(f"- {note}\n")


def apply_changes(
    runtime: RuntimeOptions,
    command: str,
    new_config_text: str,
    dry_run: bool,
    notes: list[str],
    new_catalog_text: str | None = None,
) -> int:
    before_text = read_text(runtime.config_path)
    before = snapshot_config(before_text, runtime.model_slug)
    config_changed = new_config_text != before_text
    local_catalog_changed = new_catalog_text is not None and catalog_changed(runtime.catalog_path, new_catalog_text)

    if not config_changed and not local_catalog_changed:
        print(f"{command}: no changes needed")
        return 0

    if dry_run:
        if config_changed:
            print(diff_text(before_text, new_config_text, str(runtime.config_path), f"{runtime.config_path} (planned)"))
        if local_catalog_changed:
            print(f"dry-run: would write patched catalog to {runtime.catalog_path}")
        print("dry-run: no files changed")
        return 0

    ensure_state_dirs(runtime)
    backup_path = make_backup(runtime.config_path, runtime.backup_dir)
    validation = validate_toml_if_possible(new_config_text)
    changed_files = [str(backup_path)]

    if new_catalog_text is not None and local_catalog_changed:
        write_text_atomic(runtime.catalog_path, new_catalog_text)
        changed_files.append(str(runtime.catalog_path))

    if config_changed:
        write_text_atomic(runtime.config_path, new_config_text)
        changed_files.append(str(runtime.config_path))

    after = snapshot_config(read_text(runtime.config_path), runtime.model_slug)
    changed_files.extend([str(runtime.change_record_path), str(runtime.history_path)])
    record_change(runtime, command, changed_files, backup_path, before, after, validation, notes)

    print(f"{command}: applied")
    print(f"backup: {backup_path}")
    print(validation)
    for note in notes:
        print(note)
    return 0


def recent_backups(runtime: RuntimeOptions, limit: int = 5) -> list[Path]:
    if not runtime.backup_dir.exists():
        return []
    return sorted(runtime.backup_dir.glob("config.toml.*.bak"), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def current_catalog_values(runtime: RuntimeOptions) -> tuple[int | None, int | None]:
    try:
        catalog = run_codex_debug_models(runtime)
    except Exception:
        return None, None
    model = find_model(catalog, runtime.model_slug)
    if not model:
        return None, None
    return model.get("context_window"), model.get("max_context_window")


def print_status(runtime: RuntimeOptions) -> int:
    snap = snapshot_config(read_text(runtime.config_path), runtime.model_slug)
    current_context, current_max = current_catalog_values(runtime)
    print("Codex context override status")
    print(f"- config: {runtime.config_path}")
    print(f"- model option: {runtime.model_slug}")
    print(f"- config model: {snap.model}")
    print(f"- config {COMPACT_KEY}: {snap.auto_compact_limit}")
    print(f"- config {CONTEXT_KEY}: {snap.context_window}")
    print(f"- config {CATALOG_KEY}: {snap.model_catalog_json}")
    print(f"- default catalog path: {runtime.catalog_path}")
    print(f"- config catalog {runtime.model_slug}.context_window: {snap.catalog_context_window}")
    print(f"- config catalog {runtime.model_slug}.max_context_window: {snap.catalog_max_context_window}")
    print(f"- resolved catalog {runtime.model_slug}.context_window: {current_context}")
    print(f"- resolved catalog {runtime.model_slug}.max_context_window: {current_max}")
    print(f"- expected effective window at 95%: {int(current_context * 0.95) if current_context else None}")
    print(f"- state directory: {runtime.state_dir}")
    backups = recent_backups(runtime)
    if backups:
        print("- recent backups:")
        for backup in backups:
            print(f"  - {backup}")
    else:
        print("- recent backups: none")
    return 0


def command_apply(args: argparse.Namespace) -> int:
    runtime = resolve_runtime(args, strict_limits=True)
    catalog_text, catalog_notes = patched_catalog_text(runtime, args.source_catalog)
    before_text = read_text(runtime.config_path)
    new_text, config_notes = set_root_values(
        before_text,
        {
            COMPACT_KEY: str(runtime.auto_compact_token_limit),
            CONTEXT_KEY: str(runtime.context_window),
            CATALOG_KEY: json.dumps(str(runtime.catalog_path)),
        },
    )
    return apply_changes(runtime, "apply", new_text, args.dry_run, config_notes + catalog_notes, catalog_text)


def command_clear(args: argparse.Namespace) -> int:
    runtime = resolve_runtime(args, strict_limits=False)
    before_text = read_text(runtime.config_path)
    new_text, notes = remove_root_keys(before_text, OWNED_KEYS)
    notes.append(
        f"left local catalog file inert at {runtime.catalog_path}"
        if runtime.catalog_path.exists()
        else f"no local catalog file found at {runtime.catalog_path}"
    )
    return apply_changes(runtime, "clear", new_text, args.dry_run, notes)


def command_restore(args: argparse.Namespace) -> int:
    runtime = resolve_runtime(args, strict_limits=False)
    backup = expand_path(args.backup)
    if not backup.exists() or not backup.is_file():
        print(f"restore: backup does not exist: {backup}", file=sys.stderr)
        return 2
    new_text = read_text(backup)
    return apply_changes(runtime, "restore", new_text, args.dry_run, [f"restored full config from {backup}"])


def command_status(args: argparse.Namespace) -> int:
    runtime = resolve_runtime(args, strict_limits=False)
    return print_status(runtime)


def read_root_values_if_exists(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}
    return root_value_map(read_text(config_path))


def resolve_runtime(args: argparse.Namespace, *, strict_limits: bool) -> RuntimeOptions:
    config_path = expand_path(args.config) if args.config else default_config_path()
    root = read_root_values_if_exists(config_path)
    model_slug = args.model or os.environ.get("CODEX_CONTEXT_MODEL") or root.get("model") or DEFAULT_MODEL_SLUG
    env_context_window = optional_env_int("CODEX_CONTEXT_WINDOW") if strict_limits else None
    env_auto_compact_limit = optional_env_int("CODEX_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT") if strict_limits else None
    context_window = first_defined(
        args.context_window,
        env_context_window,
        root_int(root, CONTEXT_KEY),
        DEFAULT_CONTEXT_WINDOW,
    )
    if strict_limits:
        context_window = validate_context_window(context_window, "context window")
    explicit_auto_compact_limit = args.auto_compact_token_limit
    if explicit_auto_compact_limit is None:
        explicit_auto_compact_limit = env_auto_compact_limit
    root_auto_compact_limit = root_int(root, COMPACT_KEY) if args.context_window is None and env_context_window is None else None
    auto_compact_token_limit = first_defined(
        explicit_auto_compact_limit,
        root_auto_compact_limit,
        default_auto_compact_token_limit(context_window),
    )
    if strict_limits:
        auto_compact_token_limit = validate_auto_compact_limit(
            auto_compact_token_limit,
            context_window,
            "auto-compact token limit",
        )
    catalog = args.catalog or os.environ.get("CODEX_CONTEXT_CATALOG") or root.get(CATALOG_KEY)
    return RuntimeOptions(
        config_path=config_path,
        catalog_path=expand_path(catalog) if catalog else default_catalog_path(model_slug),
        state_dir=expand_path(args.state_dir) if args.state_dir else default_state_dir(),
        codex_bin=str(expand_path(args.codex_bin)) if args.codex_bin else default_codex_bin(),
        model_slug=model_slug,
        context_window=context_window,
        auto_compact_token_limit=auto_compact_token_limit,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-context-tool",
        description="Inspect and switch portable Codex model context overrides safely.",
    )
    parser.add_argument("--config", help="Path to Codex config.toml. Default: ~/.codex/config.toml or CODEX_CONTEXT_CONFIG.")
    parser.add_argument("--catalog", help="Path to write the patched model catalog. Default: XDG config directory.")
    parser.add_argument("--state-dir", help="Directory for backups and local history. Default: XDG state directory.")
    parser.add_argument("--codex-bin", help="Path to the Codex CLI. Default: CODEX_BIN, PATH lookup, then macOS app fallback.")
    parser.add_argument("--model", help="Model slug to patch. Default: config model, CODEX_CONTEXT_MODEL, then gpt-5.5.")
    parser.add_argument(
        "--context-window",
        type=context_window_cli_int,
        help=f"Raw context_window and max_context_window value to write. Maximum: {MAX_CONTEXT_WINDOW}.",
    )
    parser.add_argument(
        "--auto-compact-token-limit",
        type=positive_cli_int,
        help=f"Root {COMPACT_KEY} value to write. Default: 90% of the selected context window.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Read-only context status report.")
    status.set_defaults(func=command_status)

    apply = subparsers.add_parser("apply", aliases=["workaround"], help="Apply a portable context override.")
    apply.add_argument("--source-catalog", help="Read source model catalog from this JSON file instead of Codex CLI.")
    apply.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    apply.set_defaults(func=command_apply)

    clear = subparsers.add_parser("clear", aliases=["official"], help="Remove this tool's root config override keys.")
    clear.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    clear.set_defaults(func=command_clear)

    restore = subparsers.add_parser("restore", help="Restore config.toml from a backup file.")
    restore.add_argument("--backup", required=True, help="Path to a config.toml backup.")
    restore.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    restore.set_defaults(func=command_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"missing file: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"codex command failed: {exc}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"invalid JSON catalog: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
