crossgame_mod = {}
local modpath = core.get_modpath("crossgame_mod")
if core.get_modpath("mcl_core") then
	dofile(modpath .. "/compat/mcl.lua")
end
