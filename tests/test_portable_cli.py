from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import codex_context_tool


TEST_ENV_NAMES = (
    "CODEX_CONTEXT_CONFIG",
    "CODEX_CONTEXT_CATALOG",
    "CODEX_CONTEXT_STATE_DIR",
    "CODEX_CONTEXT_MODEL",
    "CODEX_CONTEXT_WINDOW",
    "CODEX_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT",
    "CODEX_BIN",
)


class PortableCliTests(unittest.TestCase):
    def run_cli(self, args: list[str]) -> int:
        stdout = io.StringIO()
        stderr = io.StringIO()
        saved_env = {name: os.environ.get(name) for name in TEST_ENV_NAMES}
        try:
            for name in TEST_ENV_NAMES:
                os.environ.pop(name, None)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return codex_context_tool.main(args)
        finally:
            for name, value in saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_apply_uses_explicit_paths_and_patches_selected_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            source_catalog = root / "source-catalog.json"
            output_catalog = root / "portable-catalog.json"
            state_dir = root / "state"

            config.write_text(
                'model = "gpt-5.5"\n\n[mcp_servers.demo]\ncommand = "demo"\n',
                encoding="utf-8",
            )
            source_catalog.write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "gpt-5.5", "context_window": 272000, "max_context_window": 272000},
                            {"slug": "other-model", "context_window": 1000, "max_context_window": 1000},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--catalog",
                    str(output_catalog),
                    "--state-dir",
                    str(state_dir),
                    "--model",
                    "gpt-5.5",
                    "--context-window",
                    "1000000",
                    "--auto-compact-token-limit",
                    "900000",
                    "apply",
                    "--source-catalog",
                    str(source_catalog),
                ]
            )

            self.assertEqual(result, 0)
            config_text = config.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 1000000", config_text)
            self.assertIn("model_auto_compact_token_limit = 900000", config_text)
            self.assertIn(f'model_catalog_json = "{output_catalog}"', config_text)
            self.assertIn("[mcp_servers.demo]", config_text)

            catalog = json.loads(output_catalog.read_text(encoding="utf-8"))
            gpt55 = next(item for item in catalog["models"] if item["slug"] == "gpt-5.5")
            other = next(item for item in catalog["models"] if item["slug"] == "other-model")
            self.assertEqual(gpt55["context_window"], 1000000)
            self.assertEqual(gpt55["max_context_window"], 1000000)
            self.assertEqual(other["context_window"], 1000)
            self.assertTrue(list((state_dir / "backups").glob("config.toml.*.bak")))
            self.assertTrue((state_dir / "history.jsonl").exists())
            self.assertTrue((state_dir / "CHANGE_RECORD.md").exists())

    def test_clear_removes_only_tool_owned_root_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            state_dir = root / "state"
            config.write_text(
                "\n".join(
                    [
                        'model = "gpt-5.5"',
                        "model_context_window = 1000000",
                        "model_auto_compact_token_limit = 900000",
                        'model_catalog_json = "/tmp/catalog.json"',
                        "",
                        "[projects.demo]",
                        'trust_level = "trusted"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--state-dir",
                    str(state_dir),
                    "clear",
                ]
            )

            self.assertEqual(result, 0)
            config_text = config.read_text(encoding="utf-8")
            self.assertIn('model = "gpt-5.5"', config_text)
            self.assertIn("[projects.demo]", config_text)
            self.assertNotIn("model_context_window", config_text)
            self.assertNotIn("model_auto_compact_token_limit", config_text)
            self.assertNotIn("model_catalog_json", config_text)

    def test_apply_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            source_catalog = root / "source-catalog.json"
            output_catalog = root / "portable-catalog.json"
            state_dir = root / "state"
            config.write_text('model = "gpt-5.5"\n', encoding="utf-8")
            source_catalog.write_text(
                json.dumps({"models": [{"slug": "gpt-5.5", "context_window": 272000}]}),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--catalog",
                    str(output_catalog),
                    "--state-dir",
                    str(state_dir),
                    "apply",
                    "--source-catalog",
                    str(source_catalog),
                    "--dry-run",
                ]
            )

            self.assertEqual(result, 0)
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "gpt-5.5"\n')
            self.assertFalse(output_catalog.exists())
            self.assertFalse(state_dir.exists())

    def test_apply_discovers_model_catalog_and_limits_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            existing_catalog = root / "existing-catalog.json"
            state_dir = root / "state"
            config.write_text(
                "\n".join(
                    [
                        'model = "local-model"',
                        "model_context_window = 640000",
                        "model_auto_compact_token_limit = 600000",
                        f'model_catalog_json = "{existing_catalog}"',
                        "",
                        "[projects.demo]",
                        'trust_level = "trusted"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            existing_catalog.write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "local-model", "context_window": 128000, "max_context_window": 128000},
                            {"slug": "other-model", "context_window": 1000, "max_context_window": 1000},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--state-dir",
                    str(state_dir),
                    "--codex-bin",
                    str(root / "missing-codex"),
                    "apply",
                ]
            )

            self.assertEqual(result, 0)
            config_text = config.read_text(encoding="utf-8")
            self.assertIn('model = "local-model"', config_text)
            self.assertIn(f'model_catalog_json = "{existing_catalog}"', config_text)
            self.assertNotIn("gpt-5.5-model-catalog", config_text)
            catalog = json.loads(existing_catalog.read_text(encoding="utf-8"))
            local_model = next(item for item in catalog["models"] if item["slug"] == "local-model")
            other_model = next(item for item in catalog["models"] if item["slug"] == "other-model")
            self.assertEqual(local_model["context_window"], 640000)
            self.assertEqual(local_model["max_context_window"], 640000)
            self.assertEqual(other_model["context_window"], 1000)

    def test_custom_context_window_derives_compact_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            source_catalog = root / "source-catalog.json"
            output_catalog = root / "portable-catalog.json"
            state_dir = root / "state"
            config.write_text('model = "gpt-5.5"\n', encoding="utf-8")
            source_catalog.write_text(
                json.dumps({"models": [{"slug": "gpt-5.5", "context_window": 272000}]}),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--catalog",
                    str(output_catalog),
                    "--state-dir",
                    str(state_dir),
                    "--context-window",
                    "750000",
                    "apply",
                    "--source-catalog",
                    str(source_catalog),
                ]
            )

            self.assertEqual(result, 0)
            config_text = config.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 750000", config_text)
            self.assertIn("model_auto_compact_token_limit = 675000", config_text)

    def test_context_window_rejects_values_above_one_million(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text('model = "gpt-5.5"\n', encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    codex_context_tool.main(["--config", str(config), "--context-window", "1000001", "status"])

        self.assertEqual(raised.exception.code, 2)

    def test_apply_inserts_missing_keys_after_config_without_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            source_catalog = root / "source-catalog.json"
            output_catalog = root / "portable-catalog.json"
            state_dir = root / "state"
            config.write_text('model = "gpt-5.5"', encoding="utf-8")
            source_catalog.write_text(
                json.dumps({"models": [{"slug": "gpt-5.5", "context_window": 272000}]}),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--catalog",
                    str(output_catalog),
                    "--state-dir",
                    str(state_dir),
                    "apply",
                    "--source-catalog",
                    str(source_catalog),
                ]
            )

            self.assertEqual(result, 0)
            config_text = config.read_text(encoding="utf-8")
            self.assertIn('model = "gpt-5.5"\nmodel_auto_compact_token_limit', config_text)
            self.assertNotIn('"gpt-5.5"model_auto_compact_token_limit', config_text)

    def test_apply_keeps_added_keys_at_root_after_removing_duplicate_owned_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            source_catalog = root / "source-catalog.json"
            output_catalog = root / "portable-catalog.json"
            state_dir = root / "state"
            config.write_text(
                "\n".join(
                    [
                        "model_context_window = 1000000",
                        "model_context_window = 900000",
                        "[projects.demo]",
                        'trust_level = "trusted"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            source_catalog.write_text(
                json.dumps({"models": [{"slug": "gpt-5.5", "context_window": 272000}]}),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--catalog",
                    str(output_catalog),
                    "--state-dir",
                    str(state_dir),
                    "apply",
                    "--source-catalog",
                    str(source_catalog),
                ]
            )

            self.assertEqual(result, 0)
            root_text, table_text = config.read_text(encoding="utf-8").split("[projects.demo]", 1)
            self.assertIn("model_auto_compact_token_limit", root_text)
            self.assertIn("model_catalog_json", root_text)
            self.assertNotIn("model_auto_compact_token_limit", table_text)
            self.assertNotIn("model_catalog_json", table_text)

    def test_clear_removes_owned_keys_even_when_context_window_is_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            state_dir = root / "state"
            config.write_text(
                "\n".join(
                    [
                        "model_context_window = 1000001",
                        "model_auto_compact_token_limit = 900000",
                        f'model_catalog_json = "{root / "catalog.json"}"',
                        "",
                        "[projects.demo]",
                        'trust_level = "trusted"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--state-dir",
                    str(state_dir),
                    "clear",
                ]
            )

            self.assertEqual(result, 0)
            config_text = config.read_text(encoding="utf-8")
            self.assertNotIn("model_context_window", config_text)
            self.assertNotIn("model_auto_compact_token_limit", config_text)
            self.assertNotIn("model_catalog_json", config_text)
            self.assertIn("[projects.demo]", config_text)

    def test_env_context_window_zero_is_rejected_on_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            source_catalog = root / "source-catalog.json"
            output_catalog = root / "portable-catalog.json"
            state_dir = root / "state"
            config.write_text('model = "gpt-5.5"\n', encoding="utf-8")
            source_catalog.write_text(
                json.dumps({"models": [{"slug": "gpt-5.5", "context_window": 272000}]}),
                encoding="utf-8",
            )
            saved = os.environ.get("CODEX_CONTEXT_WINDOW")
            os.environ["CODEX_CONTEXT_WINDOW"] = "0"
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = codex_context_tool.main(
                        [
                            "--config",
                            str(config),
                            "--catalog",
                            str(output_catalog),
                            "--state-dir",
                            str(state_dir),
                            "apply",
                            "--source-catalog",
                            str(source_catalog),
                        ]
                    )
            finally:
                if saved is None:
                    os.environ.pop("CODEX_CONTEXT_WINDOW", None)
                else:
                    os.environ["CODEX_CONTEXT_WINDOW"] = saved

            self.assertEqual(result, 1)
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "gpt-5.5"\n')
            self.assertFalse(output_catalog.exists())

    def test_root_context_window_zero_is_rejected_on_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            source_catalog = root / "source-catalog.json"
            output_catalog = root / "portable-catalog.json"
            state_dir = root / "state"
            config.write_text('model = "gpt-5.5"\nmodel_context_window = 0\n', encoding="utf-8")
            source_catalog.write_text(
                json.dumps({"models": [{"slug": "gpt-5.5", "context_window": 272000}]}),
                encoding="utf-8",
            )

            result = self.run_cli(
                [
                    "--config",
                    str(config),
                    "--catalog",
                    str(output_catalog),
                    "--state-dir",
                    str(state_dir),
                    "apply",
                    "--source-catalog",
                    str(source_catalog),
                ]
            )

            self.assertEqual(result, 1)
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "gpt-5.5"\nmodel_context_window = 0\n')
            self.assertFalse(output_catalog.exists())


if __name__ == "__main__":
    unittest.main()
