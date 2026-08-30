# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Generate one API reference page per module, plus the nav for them.

Run by mkdocs-gen-files at build time. Generating rather than hand-listing means
a newly added module cannot be silently left undocumented.
"""

from pathlib import Path

import mkdocs_gen_files

PACKAGES = {
    "pb-core": "pb_core",
    "pb-client": "pb_client",
    "pbd": "pbd",
    "pb": "pb",
}

nav = mkdocs_gen_files.Nav()
root = Path(__file__).parent.parent

for dist, module in PACKAGES.items():
    src = root / "packages" / dist / "src"
    for path in sorted(src.rglob("*.py")):
        parts = list(path.relative_to(src).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        elif parts[-1].startswith("_"):
            continue  # private module
        if not parts:
            parts = [module]

        # SUMMARY.md lives inside reference/, so its nav entries are relative
        # to that directory and must not repeat the "reference/" prefix.
        rel_path = Path(*parts).with_suffix(".md")
        doc_path = Path("reference") / rel_path
        nav[parts] = rel_path.as_posix()

        with mkdocs_gen_files.open(doc_path, "w") as fd:
            fd.write(f"# `{'.'.join(parts)}`\n\n::: {'.'.join(parts)}\n")
        mkdocs_gen_files.set_edit_path(doc_path, path.relative_to(root))

with mkdocs_gen_files.open("reference/SUMMARY.md", "w") as fd:
    fd.writelines(nav.build_literate_nav())
