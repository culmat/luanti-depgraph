"""`ldg lint`: compare what a mod declares in mod.conf with what its code actually uses."""

import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from . import analyze
from .db import name_dependencies
from .luanticonf import ScannedPackage, scan_package
from .resolve import Index, Target, compat


@dataclass
class Finding:
    code: str
    severity: str  # error | warning | info
    mod: str
    message: str
    file: str | None = None
    line: int | None = None
    fix: str | None = None


def global_owners(conn: sqlite3.Connection) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    for global_name, modname in conn.execute(
            "SELECT g.global, m.name FROM mod_global g JOIN mod m ON m.id = g.mod_id"):
        owners[global_name].add(modname)
    return analyze.filter_global_owners(owners)


def known_modnames(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT modname FROM provides UNION SELECT name FROM mod")}


def lint(path: Path, conn: sqlite3.Connection | None, index: Index | None = None,
         author: str | None = None, games: list[str] | None = None) -> dict:
    pkg: ScannedPackage = scan_package(path)
    findings: list[Finding] = []
    owners = global_owners(conn) if conn else {}
    known = known_modnames(conn) if conn else set()
    own = pkg.provides
    name_deps = name_dependencies(conn) if conn else {}
    siblings = {m.name: set(m.depends) | set(m.optional_depends) for m in pkg.mods}
    all_refs: list[dict] = []

    if pkg.type in ("unknown", "txp") or not pkg.mods:
        findings.append(Finding("LDG000", "info", pkg.name,
                                f"No mods found at {pkg.root} (detected type: {pkg.type})."))
    if not any((pkg.root / n).exists() for n in ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING")):
        findings.append(Finding("LDG010", "warning", pkg.name,
                                "No LICENSE file; ContentDB requires one (FSF/OSI-approved license).",
                                fix="Add LICENSE with code and media license sections."))

    for mod in pkg.mods:
        facts = analyze.analyze_mod(mod, pkg.root)
        # Without a database, fall back to "item string prefixes are mod names".
        mod_known = known or set(facts.itemstrings) | set(facts.modpath_checks)
        declared = set(mod.depends) | set(mod.optional_depends)
        closure = analyze.dependency_closure(declared, siblings, name_deps)
        refs = analyze.references(mod, facts, owners, mod_known, own, closure)

        if not mod.has_mod_conf:
            findings.append(Finding("LDG010", "warning", mod.name, "No mod.conf.",
                                    fix=f"Create {mod.path or '.'}/mod.conf with name, description, depends."))
        elif not mod.description:
            findings.append(Finding("LDG010", "info", mod.name, "mod.conf has no description."))
        if mod.legacy_depends_txt:
            findings.append(Finding("LDG007", "warning", mod.name,
                                    "Uses deprecated depends.txt; the engine plans to drop it.",
                                    fix="Move dependencies to depends/optional_depends in mod.conf."))
        for err in facts.parse_errors[:5]:
            findings.append(Finding("LDG011", "warning", mod.name, f"Lua parse error in {err}."))
        if facts.minetest_namespace:
            site = facts.minetest_namespace[0]
            findings.append(Finding("LDG008", "info", mod.name,
                                    f"Uses the `minetest` namespace ({len(facts.minetest_namespace)}+ places);"
                                    " `core` is preferred for new code.", site.file, site.line))

        by_name: dict[str, list[analyze.Reference]] = defaultdict(list)
        for r in refs:
            by_name[r.modname].append(r)
            all_refs.append({"mod": mod.name, "modname": r.modname, "kind": r.kind, "symbol": r.symbol,
                             "count": len(r.sites), "file": r.sites[0].file, "line": r.sites[0].line,
                             "declared": r.declared, "transitive": r.transitive, "guarded": r.guarded,
                             "requires": r.requires})

        indirect = sorted(n for n, rs in by_name.items()
                          if n not in declared and any(r.transitive for r in rs) and n not in own)
        if indirect:
            findings.append(Finding("LDG012", "info", mod.name,
                                    f"Uses {', '.join(indirect[:6])}{' …' if len(indirect) > 6 else ''} only"
                                    " through other dependencies; declaring them directly is clearer."))
        for name, rs in sorted(by_name.items()):
            if name in declared or any(r.transitive for r in rs):
                continue
            sibling = " (a mod in the same package)" if name in own else ""
            guarded = any(r.guarded for r in rs)
            kinds = {r.kind for r in rs}
            first = rs[0].sites[0]
            symbols = ", ".join(sorted({r.symbol for r in rs})[:4])
            if guarded:
                findings.append(Finding(
                    "LDG002", "warning", mod.name,
                    f"Checks for '{name}'{sibling} but doesn't list it in optional_depends, so it may"
                    f" load after this mod ({symbols}).", first.file, first.line,
                    fix=f"Add {name} to optional_depends."))
            elif "global" in kinds and all(r.requires for r in rs if r.kind == "global"):
                needs = sorted({m for r in rs for m in r.requires})
                findings.append(Finding(
                    "LDG001", "warning", mod.name,
                    f"Uses '{name}'{sibling} without declaring it ({symbols}) in code loaded only when "
                    f"{', '.join(needs)} is present; that breaks in games with {', '.join(needs)} "
                    f"but without '{name}'.",
                    first.file, first.line,
                    fix=f"Add {name} to optional_depends and guard the use, or nil-check it."))
            elif "global" in kinds:
                findings.append(Finding(
                    "LDG001", "error", mod.name,
                    f"Uses '{name}'{sibling} without declaring it ({symbols}); this breaks in games"
                    f" without '{name}' and depends on load order elsewhere.", first.file, first.line,
                    fix=f"Add {name} to depends, or to optional_depends and guard the code with "
                        f"core.get_modpath(\"{name}\")."))
            elif "itemstring" in kinds:
                count = sum(len(r.sites) for r in rs if r.kind == "itemstring")
                findings.append(Finding(
                    "LDG003", "warning", mod.name,
                    f"References {count} item name(s) of '{name}'{sibling} without declaring it;"
                    f" those recipes/nodes are missing in games without '{name}'.",
                    first.file, first.line,
                    fix=f"Add {name} to optional_depends and register aliases, or use xcompat."))

        referenced = set(by_name)
        for name in sorted(declared - referenced):
            if name in mod.optional_depends:
                continue  # optional deps are often only for load order
            findings.append(Finding("LDG005", "info", mod.name,
                                    f"Declares a hard dependency on '{name}' but no use was found "
                                    "(it may be used indirectly)."))

    games_report = []
    if index is not None:
        target = Target.from_scan(pkg)
        for name in sorted(target.depends):
            if not index.providers(name):
                findings.append(Finding("LDG006", "error", pkg.name,
                                        f"Hard dependency '{name}' is not provided by any ContentDB package.",
                                        fix="Publish it, vendor it, or make it optional."))
        for mod in pkg.mods:
            others = [p for p in index.providers_of.get(mod.name, [])
                      if p.type != "game" and (author is None or p.author != author)]
            owner = next((p for p in others if p.name == mod.name), None) or (others[0] if others else None)
            if owner is not None:
                findings.append(Finding(
                    "LDG009", "warning", mod.name,
                    f"The name '{mod.name}' is already used on ContentDB by {owner.key}"
                    f"{f' and {len(others) - 1} more' if len(others) > 1 else ''}. ContentDB policy 3.1"
                    " gives a technical name to its first user.",
                    fix="Pick another name unless this is a fork or drop-in replacement (policy 3.2)."))
        game_pkgs = [index.game(g) for g in games] if games else index.default_games()
        games_report = compat(index, target, game_pkgs, all_refs)

    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f.severity], f.mod, f.code))
    return {
        "package": {"name": pkg.name, "type": pkg.type, "root": str(pkg.root),
                    "mods": [{"name": m.name, "path": m.path, "depends": m.depends,
                              "optional_depends": m.optional_depends} for m in pkg.mods],
                    "supported_games": pkg.supported_games,
                    "unsupported_games": pkg.unsupported_games},
        "findings": [asdict(f) for f in findings],
        "counts": {s: sum(f.severity == s for f in findings) for s in order},
        "refs": all_refs,
        "games": games_report,
        "database": conn is not None,
    }
