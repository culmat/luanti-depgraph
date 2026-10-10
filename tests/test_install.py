from luanti_depgraph.install import _mods_to_enable
from luanti_depgraph.luanticonf import scan_package

from .conftest import MODS


def test_only_needed_mods_of_a_modpack_are_enabled():
    pack = scan_package(MODS / "my_modpack")
    # extra_part depends on `default` (external) only; core_part stands alone.
    assert _mods_to_enable(pack, {"core_part"}) == {"core_part"}
    assert _mods_to_enable(pack, {"extra_part"}) == {"extra_part"}


def test_sibling_hard_dependencies_are_followed():
    game = scan_package(MODS / "tiny_game")
    assert _mods_to_enable(game, {"tiny_extra"}) == {"tiny_extra", "tiny_core"}
