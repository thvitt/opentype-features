# Project Overview

The `opentype-features` project is a Python-based utility designed to analyze
OpenType/TrueType font files. Its primary goal is to identify supported OpenType
features (such as ligatures, stylistic sets, and alternates) and document the
character combinations that undergo substitutions when these features are
enabled.

## Technologies

- **Language:** Python (\>= 3.14)
- **Dependency Management:** [uv](https://github.com/astral-sh/uv)
- **Core Library:** [fontTools](https://github.com/fonttools/fonttools) (for
  font table manipulation and analysis)

## Architecture

- **Source Code:** Located in `src/opentype_features/`.
- **Main Entry Point:** `src/opentype_features/__init__.py:main`.
- **Packaging:** Uses `pyproject.toml` for project metadata and script
  definitions.

# Building and Running

## Installation

Ensure you have `uv` installed. To set up the development environment and
install dependencies:

``` bash
uv sync
```

## Running the Application

The project defines a console script named `opentype-features`. You can run it
using:

``` bash
uv run opentype-features
```

Or directly via Python:

``` bash
uv run python -m opentype_features
```

## Development

To add new dependencies:

``` bash
uv add <package-name>
```

# Development Conventions

- **Tooling:** Uses `uv` for all package and environment management.
- **Project Structure:** Follows a standard `src` layout.
- **Entry Point:** The primary logic is intended to be implemented within
  `src/opentype_features/` and exposed via the `opentype-features` command.
- After all steps, make a git commit. Make sure the pre-commit checks pass.
  Outline your co-authorship in the commit message.
- Always make sure both the tests and the static analyses pass.
- Prepare tests for all new features.
