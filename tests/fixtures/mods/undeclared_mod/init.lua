undeclared_mod = {}
local S = core.get_translator("undeclared_mod")

local sounds = default.node_sound_stone_defaults()

core.register_craft({
	output = "undeclared_mod:block 4",
	recipe = {{"default:stone", "group:wood"}},
})

if core.get_modpath("mcl_core") then
	mcl_core.something()
end

local player_api = {}
player_api.foo = 1

core.register_alias("default:old_block", "undeclared_mod:block")

if stairs then
	stairs.register_stair_and_slab("block")
end

minetest.log("action", S("loaded"))
