"""Static analysis of mod Lua code to find dependencies that mod.conf doesn't declare.

For each file it collects:
  * globals the file defines (non-local assignments and global functions),
  * globals it uses (identifiers not declared local in that file),
  * item strings such as "default:stone" or ":default:stone 4",
  * core.get_modpath("x") / core.global_exists("x") guards,
  * whether it uses the deprecated `minetest` namespace.

The scope model is deliberately simple: a name declared local anywhere in a file is
treated as local throughout it. That under-reports rather than over-reports.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_lua
from tree_sitter import Language, Node, Parser

from .luanticonf import ScannedMod

_LANGUAGE = Language(tree_sitter_lua.language())
_PARSER = Parser(_LANGUAGE)

MAX_FILE_BYTES = 1_000_000

ITEMSTRING_RE = re.compile(r"^:?([a-z0-9_]+):([a-z0-9_]+)(?:\s+\d+.*)?$")
NOT_MODNAMES = {"group", "http", "https", "ftp", "file", "mailto", "data"}

LUA_GLOBALS = {
    "_G", "_VERSION", "arg", "assert", "bit", "bit32", "collectgarbage", "coroutine", "debug",
    "dofile", "error", "gcinfo", "getfenv", "getmetatable", "io", "ipairs", "jit", "load",
    "loadfile", "loadstring", "math", "module", "newproxy", "next", "os", "package", "pairs",
    "pcall", "print", "rawequal", "rawget", "rawlen", "rawset", "require", "select", "setfenv",
    "setmetatable", "string", "table", "tonumber", "tostring", "type", "unpack", "utf8",
    "xpcall", "self",
}
LUANTI_GLOBALS = {
    "core", "minetest", "vector", "ItemStack", "VoxelManip", "VoxelArea", "PseudoRandom",
    "PcgRandom", "SecureRandom", "PerlinNoise", "PerlinNoiseMap", "ValueNoise", "ValueNoiseMap",
    "Settings", "AreaStore", "Raycast", "dump", "dump2", "DIR_DELIM", "INIT", "PLATFORM",
    "nodeupdate", "fgettext", "fgettext_ne", "emerge", "insecure",
}
BUILTIN_GLOBALS = LUA_GLOBALS | LUANTI_GLOBALS

_MODPATH_CALLS = {"core.get_modpath", "minetest.get_modpath"}
# The first argument of these names an alias (often another mod's legacy item), not a dependency.
_ALIAS_CALLS = {"core.register_alias", "minetest.register_alias",
                "core.register_alias_force", "minetest.register_alias_force"}
_GLOBAL_EXISTS_CALLS = {"core.global_exists", "minetest.global_exists"}


@dataclass
class Site:
    file: str
    line: int


@dataclass
class ModFacts:
    """Everything the analyzer learned about one mod's Lua files."""
    globals_defined: set[str] = field(default_factory=set)
    # Globals assigned a table outside any function: the mod's API (e.g. `mymod = {}`).
    api_globals: set[str] = field(default_factory=set)
    globals_used: dict[str, list[Site]] = field(default_factory=lambda: defaultdict(list))
    # Globals used as `X.y`, `X:y()` or `X[k]` at least once (API access, not a stray variable).
    globals_indexed: set[str] = field(default_factory=set)
    # Globals tested for existence: `if X then`, `X and X.y`, `not X`.
    globals_tested: set[str] = field(default_factory=set)
    itemstrings: dict[str, list[Site]] = field(default_factory=lambda: defaultdict(list))
    modpath_checks: dict[str, list[Site]] = field(default_factory=lambda: defaultdict(list))
    global_exists_checks: dict[str, list[Site]] = field(default_factory=lambda: defaultdict(list))
    minetest_namespace: list[Site] = field(default_factory=list)
    # Files loaded only under `if core.get_modpath("x") then dofile(...)`: path suffix -> {"x", ...}
    conditional_loads: dict[str, set[str]] = field(default_factory=dict)
    files: int = 0
    parse_errors: list[str] = field(default_factory=list)


def _first_string_arg(call: Node) -> str | None:
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        if child.type == "string":
            content = next((c for c in child.named_children if c.type == "string_content"), None)
            return content.text.decode(errors="replace") if content is not None else ""
        return None
    return None


def _identifier_role(node: Node) -> str:
    """'use', 'local', 'global_def' or 'name' (member/label, not a variable)."""
    parent = node.parent
    if parent is None:
        return "use"
    index = next(i for i, c in enumerate(parent.children) if c.id == node.id)
    field_name = parent.field_name_for_child(index)
    ptype = parent.type
    if ptype == "dot_index_expression" and field_name == "field":
        return "name"
    if ptype == "method_index_expression" and field_name == "method":
        return "name"
    if ptype == "field" and field_name == "name":
        return "use" if parent.children[0].type == "[" else "name"
    if ptype in ("goto_statement", "label_statement", "attribute"):
        return "name"
    if ptype == "parameters":
        return "local"
    if ptype == "for_numeric_clause" and field_name == "name":
        return "local"
    if ptype == "function_declaration" and field_name == "name":
        return "local" if parent.children[0].type == "local" else "global_def"
    if ptype == "variable_list":
        grand = parent.parent
        if grand is not None and grand.type == "for_generic_clause":
            return "local"
        if grand is not None and grand.type == "assignment_statement":
            great = grand.parent
            if great is not None and great.type == "variable_declaration":
                return "local"
            return "global_def"
    return "use"


_INDEX_PARENTS = {"dot_index_expression", "method_index_expression", "bracket_index_expression"}
_CONDITION_PARENTS = {"if_statement", "elseif_statement", "while_statement", "repeat_statement"}


def _use_context(node: Node) -> tuple[bool, bool]:
    """(indexed, tested) for an identifier used as a variable."""
    parent = node.parent
    index = next(i for i, c in enumerate(parent.children) if c.id == node.id)
    field_name = parent.field_name_for_child(index)
    if parent.type in _INDEX_PARENTS:
        return field_name == "table", False
    if parent.type in _CONDITION_PARENTS:
        return False, field_name == "condition"
    if parent.type == "binary_expression":
        operators = {c.type for c in parent.children if not c.is_named}
        return False, field_name == "left" and bool(operators & {"and", "or"})
    if parent.type == "unary_expression":
        return False, any(c.type == "not" for c in parent.children)
    return False, False


_FUNCTION_SCOPES = {"function_declaration", "function_definition"}


def _within(node: Node, container: Node | None) -> bool:
    return container is not None and container.start_byte <= node.start_byte and node.end_byte <= container.end_byte


def _guard_modnames(condition: Node) -> set[str]:
    """Mod names checked with get_modpath() anywhere in an if-condition."""
    names, stack = set(), [condition]
    while stack:
        n = stack.pop()
        if n.type == "function_call":
            callee = n.child_by_field_name("name")
            if callee is not None and callee.text.decode(errors="replace") in _MODPATH_CALLS:
                arg = _first_string_arg(n)
                if arg:
                    names.add(arg)
        stack.extend(n.children)
    return names


def _enclosing_guards(node: Node) -> set[str]:
    """Mod names whose presence the enclosing if/elseif branches require for `node` to run."""
    guards: set[str] = set()
    child, parent = node, node.parent
    while parent is not None:
        if parent.type in ("if_statement", "elseif_statement") and \
                _within(child, parent.child_by_field_name("consequence")):
            condition = parent.child_by_field_name("condition")
            if condition is not None:
                guards |= _guard_modnames(condition)
        child, parent = parent, parent.parent
    return guards


def _dofile_target(call: Node) -> str | None:
    """The trailing path of dofile(modpath .. "/sub/file.lua"), e.g. "sub/file.lua"."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    last = None
    stack = list(args.named_children)
    while stack:
        n = stack.pop(0)
        if n.type == "string_content":
            last = n.text.decode(errors="replace")
        stack.extend(n.children)
    if last and last.endswith(".lua"):
        return last.lstrip("/")
    return None


def _is_api_definition(node: Node) -> bool:
    """`mymod = {}`, `mymod = mymod or {}` or `mymod = setmetatable({}, ...)` outside any function."""
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in _FUNCTION_SCOPES:
            return False
        ancestor = ancestor.parent
    variables, assignment = node.parent, node.parent.parent
    if variables.type != "variable_list" or assignment is None or assignment.type != "assignment_statement":
        return False
    position = [c.id for c in variables.named_children].index(node.id)
    values = next((c for c in assignment.named_children if c.type == "expression_list"), None)
    if values is None or position >= len(values.named_children):
        return False
    value = values.named_children[position]
    if value.type == "table_constructor":
        return True
    if value.type == "binary_expression":
        return any(c.type == "table_constructor" for c in value.named_children)
    if value.type == "function_call":
        callee = value.child_by_field_name("name")
        return callee is not None and callee.text == b"setmetatable"
    return False


def analyze_file(path: Path, rel: str, facts: ModFacts) -> None:
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        return
    tree = _PARSER.parse(data)
    facts.files += 1
    if tree.root_node.has_error:
        facts.parse_errors.append(rel)

    locals_: set[str] = set()
    uses: list[tuple[str, int, bool, bool]] = []
    defs: list[str] = []
    api_defs: list[str] = []
    alias_names: set[int] = set()  # ids of string nodes that are alias names

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        ntype = node.type
        if ntype == "identifier":
            role = _identifier_role(node)
            name = node.text.decode(errors="replace")
            if role == "local":
                locals_.add(name)
            elif role == "global_def":
                defs.append(name)
                if _is_api_definition(node):
                    api_defs.append(name)
            elif role == "use":
                indexed, tested = _use_context(node)
                uses.append((name, node.start_point[0] + 1, indexed, tested))
            continue
        if ntype == "string":
            if node.id in alias_names:
                continue
            content = next((c for c in node.named_children if c.type == "string_content"), None)
            if content is not None:
                text = content.text.decode(errors="replace").strip()
                match = ITEMSTRING_RE.match(text)
                if match and match.group(1) not in NOT_MODNAMES:
                    facts.itemstrings[match.group(1)].append(Site(rel, node.start_point[0] + 1))
            continue
        if ntype == "function_call":
            callee = node.child_by_field_name("name")
            if callee is not None and callee.type == "identifier" and callee.text == b"dofile":
                target = _dofile_target(node)
                guards = _enclosing_guards(node) if target else set()
                if target and guards:
                    facts.conditional_loads.setdefault(target, set()).update(guards)
            if callee is not None and callee.type == "dot_index_expression":
                callee_text = callee.text.decode(errors="replace")
                if callee_text in _MODPATH_CALLS:
                    arg = _first_string_arg(node)
                    if arg:
                        facts.modpath_checks[arg].append(Site(rel, node.start_point[0] + 1))
                elif callee_text in _ALIAS_CALLS:
                    args = node.child_by_field_name("arguments")
                    first = args.named_children[0] if args is not None and args.named_children else None
                    if first is not None and first.type == "string":
                        alias_names.add(first.id)
                elif callee_text in _GLOBAL_EXISTS_CALLS:
                    arg = _first_string_arg(node)
                    if arg:
                        facts.global_exists_checks[arg].append(Site(rel, node.start_point[0] + 1))
        stack.extend(reversed(node.children))

    for name in defs:
        if name not in locals_:
            facts.globals_defined.add(name)
    for name in api_defs:
        if name not in locals_:
            facts.api_globals.add(name)
    for name, line, indexed, tested in uses:
        if name in locals_:
            continue
        if indexed:
            facts.globals_indexed.add(name)
        if tested:
            facts.globals_tested.add(name)
        if name == "minetest":
            if len(facts.minetest_namespace) < 50:
                facts.minetest_namespace.append(Site(rel, line))
            continue
        if name in BUILTIN_GLOBALS:
            continue
        facts.globals_used[name].append(Site(rel, line))


def analyze_mod(mod: ScannedMod, package_root: Path) -> ModFacts:
    facts = ModFacts()
    for path in sorted(mod.dir.rglob("*.lua")):
        rel_parts = path.relative_to(mod.dir).parts
        # Skip nested mods (handled separately) and test/vendor-ish folders that aren't loaded.
        if any(p.startswith(".") for p in rel_parts):
            continue
        if len(rel_parts) > 1 and (mod.dir / rel_parts[0] / "init.lua").is_file() \
                and (mod.dir / rel_parts[0]).resolve() != mod.dir.resolve():
            continue
        if rel_parts[0] in ("spec", "test", "tests", "mtt"):
            continue
        rel = path.relative_to(package_root).as_posix()
        try:
            analyze_file(path, rel, facts)
        except (OSError, ValueError) as exc:
            facts.parse_errors.append(f"{rel}: {exc}")
    return facts


AMBIGUOUS_GLOBAL_OWNERS = 3


def filter_global_owners(owners: dict[str, set[str]]) -> dict[str, set[str]]:
    """Accidental globals like `pos` or `i` are defined by many unrelated mods; ignore those
    unless the global is named like the mod (the `default`/`mcl_core` convention)."""
    filtered = {g: (n if len(n) <= AMBIGUOUS_GLOBAL_OWNERS else n & {g}) for g, n in owners.items()}
    return {g: n for g, n in filtered.items() if n}


@dataclass
class Reference:
    modname: str
    kind: str  # global | itemstring | modpath_guard
    symbol: str
    sites: list[Site]
    declared: bool  # listed in this mod's depends/optional_depends
    transitive: bool  # not listed, but pulled in (and loaded earlier) through a declared dependency
    guarded: bool  # the mod checks get_modpath()/global_exists() for it
    requires: list[str] = field(default_factory=list)  # mods its file needs to be loaded at all


def dependency_closure(start: set[str], siblings: dict[str, set[str]],
                       name_deps: dict[str, set[str]], limit: int = 500) -> set[str]:
    """Mod names that load before a mod declaring `start`: its dependencies, theirs, and so on.
    `siblings` (mods of the same package) take precedence over `name_deps` (everything else)."""
    seen = set(start)
    queue = list(start)
    while queue and len(seen) < limit:
        name = queue.pop()
        for dep in siblings[name] if name in siblings else name_deps.get(name, ()):
            if dep not in seen:
                seen.add(dep)
                queue.append(dep)
    return seen


def file_requirements(facts: ModFacts, mod_rel_prefix: str) -> dict[str, frozenset[str]]:
    """Map package-relative file paths to the mods their conditional loading requires."""
    out: dict[str, frozenset[str]] = {}
    prefix = f"{mod_rel_prefix}/" if mod_rel_prefix else ""
    for target, guards in facts.conditional_loads.items():
        out[prefix + target] = frozenset(guards)
    return out


def references(mod: ScannedMod, facts: ModFacts, global_owners: dict[str, set[str]],
               known_modnames: set[str], own_names: set[str], closure: set[str]) -> list[Reference]:
    """Turn raw facts into references to other mod names.

    `global_owners` maps an API global to the mod names known to define it. A global nobody
    is known to define but that equals a known mod name is assumed to be that mod's.
    `own_names` are the mods of the same package: references to them are reported too
    (load order matters inside modpacks and games), but never references to the mod itself.
    `closure` is the transitive dependency closure of the mod's declared dependencies.
    References in files that are only loaded when some mods are present (dofile inside
    `if core.get_modpath("x")`) carry those mods in `requires`.
    """
    declared = set(mod.depends) | set(mod.optional_depends)
    guarded_names = set(facts.modpath_checks)
    for global_name in facts.global_exists_checks.keys() | facts.globals_tested:
        guarded_names |= global_owners.get(global_name, {global_name} & known_modnames)
    requirements = file_requirements(facts, mod.path)
    refs: list[Reference] = []

    def emit(modname: str, kind: str, symbol: str, sites: list[Site], guarded: bool) -> None:
        groups: dict[frozenset[str], list[Site]] = {}
        for site in sites:
            groups.setdefault(requirements.get(site.file, frozenset()), []).append(site)
        for requires, group in groups.items():
            refs.append(Reference(modname, kind, symbol, group, modname in declared,
                                  modname not in declared and modname in closure,
                                  guarded or modname in requires, sorted(requires)))

    for global_name, sites in facts.globals_used.items():
        # A bare read of a stray global (`title`, `items`) says nothing about other mods' APIs.
        if global_name in facts.globals_defined or global_name not in facts.globals_indexed:
            continue
        identity = {global_name} & (known_modnames | own_names)
        owners = identity | global_owners.get(global_name, set())
        if not owners or mod.name in owners:
            continue
        chosen = (owners & declared) or (owners & closure) or identity or owners
        tested = global_name in facts.global_exists_checks or global_name in facts.globals_tested
        for owner in sorted(chosen):
            emit(owner, "global", global_name, sites, owner in guarded_names or tested)

    for modname, sites in facts.itemstrings.items():
        if modname == mod.name or modname not in (known_modnames | own_names):
            continue
        emit(modname, "itemstring", f"{modname}:*", sites, modname in guarded_names)

    for modname, sites in facts.modpath_checks.items():
        if modname == mod.name:
            continue
        emit(modname, "modpath_guard", f'get_modpath("{modname}")', sites, True)
    return refs
