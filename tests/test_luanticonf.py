from luanti_depgraph.luanticonf import parse_conf, scan_package, split_games, split_names

from .conftest import MODS


def test_parse_conf_multiline_and_comments():
    conf = parse_conf('# comment\nname = x\ndescription = """line one\nline two"""\ndepends = a, b ,c\n')
    assert conf["name"] == "x"
    assert conf["description"] == "line one\nline two"
    assert split_names(conf["depends"]) == ["a", "b", "c"]


def test_split_games_strips_game_suffix():
    assert split_games("*, minetest_game, mineclonia") == ["*", "minetest", "mineclonia"]


def test_scan_mod_with_mod_conf():
    pkg = scan_package(MODS / "undeclared_mod")
    assert pkg.type == "mod"
    assert [m.name for m in pkg.mods] == ["undeclared_mod"]
    assert pkg.hard_depends == {"xcompat"}
    assert pkg.optional_depends == {"stairs"}


def test_scan_legacy_depends_txt():
    mod = scan_package(MODS / "legacy_mod").mods[0]
    assert mod.legacy_depends_txt
    assert mod.depends == ["default"]
    assert mod.optional_depends == ["farming"]


def test_scan_modpack_drops_self_provided_names():
    pkg = scan_package(MODS / "my_modpack")
    assert pkg.type == "modpack"
    assert pkg.provides == {"core_part", "extra_part"}
    assert pkg.hard_depends == {"default"}
    assert pkg.supported_games == ["minetest", "mineclonia"]


def test_scan_game_with_nested_modpack():
    pkg = scan_package(MODS / "tiny_game")
    assert pkg.type == "game"
    assert pkg.name == "tiny"
    assert pkg.provides == {"tiny_core", "tiny_extra"}
    assert pkg.hard_depends == set()
