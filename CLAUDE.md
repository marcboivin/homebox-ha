# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build/Lint/Test Commands
- Install: `pip install -e .`
- Lint: `flake8 custom_components/homebox`
- Type check: `mypy custom_components/homebox`
- Test: `pytest tests/`
- Single test: `pytest tests/test_file.py::test_function -v`

## Code Style Guidelines
- Follow Home Assistant custom component guidelines
- PEP 8 style with 4-space indentation
- Type hints required for all functions
- Import order: stdlib, third-party, Home Assistant, local
- Use async/await for all I/O operations
- Error handling: use try/except with specific exceptions
- Naming: snake_case for variables/functions, PascalCase for classes
- Document public APIs with docstrings
- Prefix private methods with underscore