from luanti_depgraph import analyze
from luanti_depgraph.luanticonf import scan_package

from .conftest import MODS


def _refs(known=frozenset({"default", "mcl_core", "stairs", "player_api", "xcompat"})):
    pkg = scan_package(MODS / "undeclared_mod")
    mod = pkg.mods[0]
    facts = analyze.analyze_mod(mod, pkg.root)
    closure = analyze.dependency_closure(set(mod.depends) | set(mod.optional_depends), {}, {})
    return facts, analyze.references(mod, facts, {}, set(known), pkg.provides, closure)


def test_facts():
    facts, _ = _refs()
    assert "undeclared_mod" in facts.globals_defined
    assert "undeclared_mod" in facts.api_globals
    assert "default" in facts.globals_indexed
    assert "stairs" in facts.globals_tested
    assert set(facts.modpath_checks) == {"mcl_core"}
    assert facts.minetest_namespace, "minetest.log should be noticed"


def test_undeclared_global_and_itemstring():
    _, refs = _refs()
    by = {(r.modname, r.kind): r for r in refs}
    assert not by[("default", "global")].declared
    assert not by[("default", "global")].guarded
    assert ("default", "itemstring") in by


def test_alias_names_are_not_dependencies():
    _, refs = _refs()
    default_items = [r for r in refs if r.modname == "default" and r.kind == "itemstring"]
    assert len(default_items[0].sites) == 1  # only "default:stone"; "default:old_block" is an alias


def test_guarded_and_declared():
    _, refs = _refs()
    by = {(r.modname, r.kind): r for r in refs}
    assert by[("mcl_core", "global")].guarded
    assert by[("stairs", "global")].declared


def test_local_shadowing_is_not_a_reference():
    _, refs = _refs()
    assert not [r for r in refs if r.modname == "player_api"]


def test_ambiguous_globals_are_filtered():
    owners = {"pos": {"a", "b", "c", "d"}, "mymod": {"mymod", "x", "y", "z"}, "api": {"a"}}
    assert analyze.filter_global_owners(owners) == {"mymod": {"mymod"}, "api": {"a"}}


def test_dependency_closure_prefers_siblings():
    closure = analyze.dependency_closure({"a"}, {"a": {"b"}}, {"a": {"zzz"}, "b": {"c"}})
    assert closure == {"a", "b", "c"}
