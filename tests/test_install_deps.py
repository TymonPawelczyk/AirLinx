"""Every third-party module that airlinx/ imports must be installed by install.sh (directly or transitively),
otherwise a fresh `./install.sh` produces an app that crashes on first use. gi comes from the system packages."""
import ast
import importlib.metadata as md
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROVIDED = {"gi"}


def declared_deps():
    text = (ROOT / "install.sh").read_text()
    m = re.search(r"^DEPS=\(([^)]*)\)", text, re.M)
    assert m, "DEPS array not found in install.sh"
    return m.group(1).split()


def third_party_imports():
    found = {}
    for py in (ROOT / "airlinx").rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for n in names:
                top = n.split(".")[0]
                if top not in sys.stdlib_module_names and top not in ("airlinx", "__future__"):
                    found.setdefault(top, py.name)
    return found


def closure(dists):
    seen, todo = set(), [d.lower().replace("_", "-") for d in dists]
    while todo:
        d = todo.pop()
        if d in seen:
            continue
        seen.add(d)
        try:
            reqs = md.requires(d) or []
        except md.PackageNotFoundError:
            continue
        for r in reqs:
            if "extra ==" in r:
                continue
            todo.append(re.split(r"[ ;<>=!~\[(]", r, 1)[0].lower().replace("_", "-"))
    return seen


def test_every_third_party_import_is_covered_by_install_deps():
    deps = declared_deps()
    assert {"aiohttp", "cryptography", "zeroconf"} <= set(deps)
    have = closure(deps)
    owners = md.packages_distributions()
    missing = []
    for mod, where in sorted(third_party_imports().items()):
        if mod in SYSTEM_PROVIDED:
            continue
        dists = {d.lower().replace("_", "-") for d in owners.get(mod, [])}
        if not dists:
            dists = {mod.lower().replace("_", "-")}      # compiled packages may lack top_level.txt: name == module
        if not dists & have:
            missing.append(f"{mod} ({where}) -> {sorted(dists)}")
    assert not missing, f"imported but not installed by install.sh: {missing}"


def test_requirements_dev_lists_what_the_tests_use():
    dev = (ROOT / "requirements-dev.txt").read_text().lower()
    for name in ("pytest", "pytest-asyncio", "pytest-cov", "playwright"):
        assert re.search(rf"^{name}\b", dev, re.M), name


def test_install_deps_have_no_unused_package():
    """Each DEPS entry is imported by airlinx/ or required by a package that is (no dead weight on users' disks,
    no extra licenses to carry: samsungtvws was installed for years without a single import)."""
    norm = lambda d: d.lower().replace("_", "-")  # noqa: E731
    owners = md.packages_distributions()
    imported = set()
    for mod in third_party_imports():
        imported |= {norm(d) for d in owners.get(mod, [])} or {norm(mod)}
    needed = closure(imported)
    unused = [d for d in declared_deps() if norm(d) not in needed]
    assert not unused, f"install.sh installs packages nothing uses: {unused}"


def test_requirements_dev_has_no_unused_runtime_package():
    dev = (ROOT / "requirements-dev.txt").read_text().lower()
    assert not re.search(r"^samsungtvws\b", dev, re.M)
