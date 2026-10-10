from luanti_depgraph.codesearch import chunk_source

SOURCE = b"""local S = core.get_translator("m")
m = {}

function m.helper(pos)
	return core.get_node(pos)
end

core.register_node("m:stone", {
	description = S("Stone"),
	groups = {cracky = 3},
})

local defaults = {
	a = 1,
}

print("x")
"""


def test_chunks_follow_lua_structure():
    chunks = chunk_source(SOURCE)
    kinds = [(c["kind"], c["symbol"]) for c in chunks]
    assert ("function", "m.helper") in kinds
    assert ("register", "register_node m:stone") in kinds
    assert ("table", "defaults") in kinds
    register = next(c for c in chunks if c["kind"] == "register")
    assert (register["line_start"], register["line_end"]) == (8, 11)
    assert any(c["kind"] == "code" for c in chunks)
