import pytest

from luanti_depgraph import queries
from luanti_depgraph.resolve import Resolver, Target

from .conftest import MODS


def test_counts_and_aliases(index):
    assert len(index.by_id) == 10
    assert index.package("Minetest/minetest_game").key == "Luanti/minetest_game"


def test_game_lookup_normalizes(index):
    assert index.game("minetest").key == "Luanti/minetest_game"
    assert index.game("VoxeLibre").key == "Wuzzy/mineclone2"
    with pytest.raises(LookupError):
        index.game("pipeworks")


def test_name_only_package_lookup_prefers_score(index):
    assert index.package("xcompat").key == "other/xcompat"


def test_providers_prefer_exact_name_then_score(index):
    assert [p.key for p in index.providers("xcompat")] == ["other/xcompat", "mt-mods/xcompat"]


def test_resolve_installs_dependencies(index):
    res = Resolver(index, index.game("minetest_game")).resolve(Target.from_pkg(index.package("mt-mods/pipeworks")))
    assert res.ok
    assert [e["package"] for e in res.install] == ["other/xcompat"]
    assert "default" in res.from_game


def test_resolve_falls_back_to_provider_that_works_in_the_game(index):
    # other/xcompat needs `default`, which Mineclonia lacks; mt-mods/xcompat works anywhere.
    res = Resolver(index, index.game("mineclonia")).resolve(Target.from_pkg(index.package("mt-mods/pipeworks")))
    assert res.ok
    assert [e["package"] for e in res.install] == ["mt-mods/xcompat"]


def test_resolve_explains_game_only_dependencies(index):
    res = Resolver(index, index.game("mineclonia")).resolve(Target.from_pkg(index.package("TenPlus1/ethereal")))
    assert not res.ok
    missing = {m["modname"]: m for m in res.missing}
    assert set(missing) == {"default", "stairs"}
    assert missing["default"]["only_in_games"] == ["Luanti/minetest_game"]


def test_cycles_terminate(index):
    res = Resolver(index, index.game("minetest_game")).resolve(Target.from_pkg(index.package("someone/cycle_a")))
    assert not res.ok


def test_local_mod_named_like_a_game_mod_is_flagged(index, tmp_path):
    mod = tmp_path / "default"
    mod.mkdir()
    (mod / "init.lua").write_text("")
    res = queries.resolve(index, str(mod), "minetest_game")
    assert res["shadowed"][0]["modname"] == "default"


def test_compat_matrix(index):
    rows = {r["game"]: r for r in queries.compat_report(index, "TenPlus1/ethereal")["games"]}
    assert rows["Luanti/minetest_game"]["status"] == "ok"
    assert rows["ryvnf/mineclonia"]["status"] == "unsatisfiable"


def test_rdeps(index):
    direct = queries.rdeps(index, "default", hard_only=True)
    assert {r["package"] for r in direct["dependents"]} == {"TenPlus1/ethereal", "other/xcompat"}
    transitive = queries.rdeps(index, "default", transitive=True, hard_only=True)
    assert "mt-mods/pipeworks" in {r["package"] for r in transitive["dependents"]}


def test_name_check(index):
    assert queries.name_check(index, "pipeworks")["available"] is False
    assert queries.name_check(index, "brand_new_mod")["available"] is True
    assert queries.name_check(index, "Bad-Name")["valid"] is False
    assert queries.name_check(index, "default")["bundled_in_games"] == ["Luanti/minetest_game"]


def test_deps_tree_local(index):
    data = queries.deps(index, str(MODS / "my_modpack"), tree=True)
    assert data["type"] == "modpack"
    assert data["tree"][0]["modname"] == "default"
    assert data["tree"][0]["via"] is None


def test_blockers_ranks_root_causes(index):
    data = queries.blockers(index, "mineclonia")
    assert data["game"] == "ryvnf/mineclonia"
    assert data["mods"] == 6
    # ethereal (default, stairs), other/xcompat (default), cycle_a and cycle_b (nonexistent)
    assert data["unsatisfiable"] == 4
    roots = {r["modname"]: r for r in data["root_causes"]}
    assert roots["default"]["blocked"] == 2
    assert roots["default"]["sole_blocker"] == 1
    assert roots["default"]["mod_providers"] == 0
    assert roots["default"]["game_providers"] == 1
    assert roots["nonexistent"]["blocked"] == 2
    assert "cycle_a" not in roots and "cycle_b" not in roots
    missing = {r["modname"]: r for r in data["missing"]}
    assert missing["default"]["blocked"] == 2
    assert "cycle_b" in missing


def test_blockers_respects_what_the_game_bundles(index):
    roots = {r["modname"] for r in queries.blockers(index, "minetest_game")["root_causes"]}
    assert roots == {"nonexistent"}
    with pytest.raises(LookupError):
        queries.blockers(index, "no_such_game")
