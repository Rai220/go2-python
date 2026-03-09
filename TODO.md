# Refactoring Plan: CLI tool + AI Skill

## Goal
Make go2-python a publishable CLI tool (`go2`) and AI skill for Claude Code / Cursor.

## Steps

- [x] Create `pyproject.toml` with entry point `go2 = "go2.cli:main"`
- [x] Move `main.py` → `go2/cli.py` (add `serve` subcommand, remove `interactive`)
- [x] Move `server.py` → `go2/server.py`
- [x] Update `go2/__init__.py` (add `__version__`)
- [x] Delete `lidar_snapshot.py`, `lidar_test.py`, `lidar_visualize.py` (experimental, not part of CLI)
- [x] Delete `AGENTS.md` (redundant, replaced by CLAUDE.md)
- [x] Rewrite `CLAUDE.md` as AI skill (English, safety rules, CLI/curl usage)
- [x] Rewrite `README.md` (English, installation, usage, skill docs)
- [ ] Add project license later
- [x] Add `.env.example`
- [x] Update `.gitignore`
- [x] Keep `web.py` + `web/` (legacy debugging UI, will be developed further)
