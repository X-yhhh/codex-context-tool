# Changelog

## 0.2.0

- Converted the original machine-specific GPT-5.5 workaround into a portable
  Codex context override CLI.
- Added configurable config, catalog, state, Codex binary, model, context
  window, and auto-compact paths/values.
- Added local Codex config discovery for existing model, context, auto-compact,
  and model catalog values, with a 1M maximum for custom context windows.
- Moved runtime backups and history out of the project checkout into an XDG
  state directory.
- Added a Python package entry point and command-line wrapper.
- Added unit tests that exercise temporary config and catalog files only.
