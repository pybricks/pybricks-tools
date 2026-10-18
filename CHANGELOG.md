# Changelog

Repository-wide changes: tooling, CI and documentation. The packages are
released independently and each keeps its own changelog:

- [`pybricks-core`](packages/pb-core/CHANGELOG.md)
- [`pybricks-client`](packages/pb-client/CHANGELOG.md)
- [`pybricks-service`](packages/pb-service/CHANGELOG.md)
- [`pybricks-cli`](packages/pb-cli/CHANGELOG.md)

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- A uv workspace of four packages, `src` layout throughout, with `py.typed`.
- ruff, pyright strict, pytest and a strict docs build, gated from commit one.
- CI on Linux, macOS, Windows and Windows ARM64, across Python 3.11 to 3.14.
- Documentation with the API and command references generated from the code.

### Notes

- Docs are built with ProperDocs. MkDocs 2.0 drops the plugin and theming
  systems with no migration path; ProperDocs continues the 1.6 line, so
  Material, mkdocstrings, gen-files and literate-nav all load unchanged.
- The Windows ARM64 `mpy-cross-v6` wheel contains an x86-64 binary, so the
  compiler runs under emulation there. The resolved path is logged at startup.
