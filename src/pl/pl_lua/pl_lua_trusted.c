/*-------------------------------------------------------------------------
 *
 * pl_lua_trusted.c
 *	  The trusted sandbox, and the pl_lua.trusted and pl_lua.paths modules.
 *
 * Most of the sandbox logic (what is copied into it, its require and load,
 * the pl_lua.trusted functions) is written in Lua, in the string below,
 * which is run once for each interpreter.  It keeps its state in upvalues,
 * so nothing in it can be reached from sandboxed code.
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/pl/pl_lua/pl_lua_trusted.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "miscadmin.h"
#include "port.h"

#include "pl_lua_int.h"

/* a function that finds an installation directory */
typedef void (*pll_path_fn) (const char *my_exec_path, char *ret_path);

typedef struct pll_path_entry
{
	const char *name;
	pll_path_fn fn;
} pll_path_entry;

static void pll_bin_path(const char *my_exec_path, char *ret_path);
static int	pll_path_func(lua_State * L);

static const pll_path_entry pll_paths[] = {
	{"bin", pll_bin_path},
	{"lib", get_lib_path},
	{"pkglib", get_pkglib_path},
	{"libdir", get_pkglib_path},
	{"share", get_share_path},
	{"locale", get_locale_path},
	{"doc", get_doc_path},
	{"etc", get_etc_path},
	{"html", get_html_path},
	{"include", get_include_path},
	{"includeserver", get_includeserver_path},
	{"man", get_man_path},
	{"pkginclude", get_pkginclude_path},
	{NULL, NULL}
};

static const char pll_trusted_lua[] =
"-- Support code for the pl_lua.trusted module and the sandbox.  It is run\n"
"-- once per interpreter, with the global \"require\" as its argument, and\n"
"-- returns the pl_lua.trusted module and a table of internal functions.\n"
"local realrequire = ...\n"
"local type, pairs, ipairs, next, error, select, tostring =\n"
"	type, pairs, ipairs, next, error, select, tostring\n"
"local setmetatable, getmetatable, realload =\n"
"	setmetatable, getmetatable, load\n"
"local concat = table.concat\n"
"\n"
"local allowed = {}			-- name inside the sandbox -> entry\n"
"local removals = {}			-- removals requested before the sandbox exists\n"
"local sandbox = nil\n"
"local M = {}\n"
"local U = {}\n"
"\n"
"local function copy(v, deep, seen)\n"
"	if type(v) ~= \"table\" then return v end\n"
"	if seen[v] then return seen[v] end\n"
"	local c = {}\n"
"	seen[v] = c\n"
"	for k, x in next, v do\n"
"		if deep then x = copy(x, true, seen) end\n"
"		c[k] = x\n"
"	end\n"
"	return c\n"
"end\n"
"\n"
"local function proxy(v, deep, cache)\n"
"	if type(v) ~= \"table\" then return v end\n"
"	if cache[v] then return cache[v] end\n"
"	local p = {}\n"
"	local mt = {}\n"
"	cache[v] = p\n"
"	if deep then\n"
"		mt.__index = function(_, k)\n"
"			local x = v[k]\n"
"			if type(x) == \"table\" then return proxy(x, true, cache) end\n"
"			return x\n"
"		end\n"
"		mt.__pairs = function()\n"
"			local function iter(_, k)\n"
"				local nk, nv = next(v, k)\n"
"				if type(nv) == \"table\" then nv = proxy(nv, true, cache) end\n"
"				return nk, nv\n"
"			end\n"
"			return iter, p, nil\n"
"		end\n"
"	else\n"
"		mt.__index = v\n"
"		mt.__pairs = function() return next, v, nil end\n"
"	end\n"
"	mt.__call = function(_, ...) return v(...) end\n"
"	mt.__len = function() return #v end\n"
"	mt.__metatable = false\n"
"	setmetatable(p, mt)\n"
"	return p\n"
"end\n"
"\n"
"local modes = {\n"
"	direct = true, copy = true, scopy = true, proxy = true, sproxy = true\n"
"}\n"
"\n"
"-- the value to show the sandbox for an allowed module\n"
"local function expose(e)\n"
"	if e.exposed ~= nil then return e.exposed end\n"
"	if e.real == nil then e.real = realrequire(e.module) end\n"
"	local real, mode, v = e.real, e.mode\n"
"	if type(mode) == \"function\" then\n"
"		v = mode(real)\n"
"	elseif type(real) ~= \"table\" or mode == \"direct\" then\n"
"		v = real\n"
"	elseif mode == \"copy\" then\n"
"		v = copy(real, true, {})\n"
"	elseif mode == \"scopy\" then\n"
"		v = copy(real, false, {})\n"
"	elseif mode == \"proxy\" then\n"
"		v = proxy(real, true, {})\n"
"	else\n"
"		v = proxy(real, false, {})\n"
"	end\n"
"	e.exposed = v\n"
"	return v\n"
"end\n"
"\n"
"local function forget(name, global)\n"
"	allowed[name] = nil\n"
"	if sandbox then\n"
"		sandbox.package.loaded[name] = nil\n"
"		if global == true then\n"
"			sandbox[name] = nil\n"
"		elseif type(global) == \"string\" then\n"
"			sandbox[global] = nil\n"
"		end\n"
"	else\n"
"		removals[#removals + 1] = { name, global }\n"
"	end\n"
"end\n"
"\n"
"local function allow1(module, name, mode, global, preload)\n"
"	if type(module) ~= \"string\" then\n"
"		error(\"module name must be a string\", 3)\n"
"	end\n"
"	if name == nil then name = module end\n"
"	if mode == nil then mode = \"proxy\" end\n"
"	if type(name) ~= \"string\" then\n"
"		error(\"new module name must be a string\", 3)\n"
"	end\n"
"	if type(mode) ~= \"function\" and not modes[mode] then\n"
"		error(\"invalid module mode\", 3)\n"
"	end\n"
"	if global ~= nil and global ~= true and global ~= false\n"
"		and type(global) ~= \"string\" then\n"
"		error(\"global must be a boolean or a string\", 3)\n"
"	end\n"
"	local e = { module = module, mode = mode }\n"
"	allowed[name] = e\n"
"	if sandbox then sandbox.package.loaded[name] = nil end\n"
"	if global == true then\n"
"		e.global = name\n"
"	elseif global then\n"
"		e.global = global\n"
"	end\n"
"	if e.global or preload then e.real = realrequire(module) end\n"
"	if e.global and sandbox then sandbox[e.global] = expose(e) end\n"
"end\n"
"\n"
"function M.allow(module, name, mode, global, preload)\n"
"	if type(module) ~= \"table\" then\n"
"		return allow1(module, name, mode, global, preload)\n"
"	end\n"
"	for _, item in ipairs(module) do\n"
"		if type(item) == \"table\" then\n"
"			local n, m, g, p = item[2], item[3], item[4], item[5]\n"
"			if n == nil then n = name end\n"
"			if m == nil then m = mode end\n"
"			if g == nil then g = global end\n"
"			if p == nil then p = preload end\n"
"			allow1(item[1], n, m, g, p)\n"
"		else\n"
"			allow1(item, name, mode, global, preload)\n"
"		end\n"
"	end\n"
"end\n"
"\n"
"function M.require(module, name, mode)\n"
"	return M.allow(module, name, mode, true)\n"
"end\n"
"\n"
"function M.remove(name, global)\n"
"	if type(name) ~= \"string\" then\n"
"		error(\"module name must be a string\", 2)\n"
"	end\n"
"	forget(name, global)\n"
"end\n"
"\n"
"-- Build the sandbox.  G is the real global environment, loaded the real\n"
"-- package.loaded.\n"
"function U.build(G, loaded, install_globals)\n"
"	local S = {}\n"
"	local function pick(from, names)\n"
"		local t = {}\n"
"		for _, n in ipairs(names) do t[n] = from[n] end\n"
"		return t\n"
"	end\n"
"	local function shallow(from, omit)\n"
"		local t = {}\n"
"		for k, v in next, from do\n"
"			if k ~= omit then t[k] = v end\n"
"		end\n"
"		return t\n"
"	end\n"
"\n"
"	for _, n in ipairs { \"assert\", \"collectgarbage\", \"error\", \"ipairs\",\n"
"		\"next\", \"pairs\", \"rawequal\", \"rawlen\", \"rawget\", \"rawset\",\n"
"		\"select\", \"tonumber\", \"tostring\", \"type\", \"warn\",\n"
"		\"print\", \"pcall\", \"xpcall\", \"lpcall\",\n"
"		\"_VERSION\", \"_PLVERSION\", \"_PLREVISION\", \"_PG_VERSION\",\n"
"		\"_PG_VERSION_NUM\", \"_PL_IDENT\", \"_PL_LOAD_TIME\" } do\n"
"		S[n] = G[n]\n"
"	end\n"
"	S._G = S\n"
"\n"
"	S.coroutine = shallow(G.coroutine)\n"
"	S.math = shallow(G.math)\n"
"	S.table = shallow(G.table)\n"
"	S.utf8 = shallow(G.utf8)\n"
"	S.string = shallow(G.string, \"dump\")\n"
"	S.os = pick(G.os, { \"clock\", \"date\", \"difftime\", \"time\" })\n"
"\n"
"	-- Strings share one metatable, which refers to the real string table.\n"
"	-- Don't let the sandbox reach it.\n"
"	local strmt = { __index = S.string }\n"
"	S.getmetatable = function(o)\n"
"		if type(o) == \"string\" then return strmt end\n"
"		return getmetatable(o)\n"
"	end\n"
"\n"
"	-- A finalizer (a __gc metamethod) is run by the garbage collector, which\n"
"	-- can start at any Lua allocation, including those made by PL/Lua's own\n"
"	-- C functions while they work with the database.  So trusted code is not\n"
"	-- allowed to set one.  A metatable gets its finalizer only if it has the\n"
"	-- field when it is set on an object, so this is the only place to check.\n"
"	-- __close, for to-be-closed variables, is available for cleanup.\n"
"	local rawget, lpcall, gsub = G.rawget, G.lpcall, G.string.gsub\n"
"	S.setmetatable = function(t, mt)\n"
"		if type(t) ~= \"table\" then\n"
"			error(\"bad argument #1 to 'setmetatable' (table expected, got \" ..\n"
"				type(t) .. \")\", 2)\n"
"		end\n"
"		if mt ~= nil and type(mt) ~= \"table\" then\n"
"			error(\"bad argument #2 to 'setmetatable' (nil or table expected)\", 2)\n"
"		end\n"
"		if mt ~= nil and rawget(mt, \"__gc\") ~= nil then\n"
"			error(\"a metatable with a __gc field is not allowed in trusted code\", 2)\n"
"		end\n"
"		-- errors of the real function name this chunk as their position\n"
"		local ok, r = lpcall(setmetatable, t, mt)\n"
"		if ok then return r end\n"
"		error((gsub(r, \"^[^:]*:%d+: \", \"\")), 2)\n"
"	end\n"
"\n"
"	S.load = function(chunk, chunkname, mode, ...)\n"
"		local env = S\n"
"		if select(\"#\", ...) > 0 then env = ... end\n"
"		return realload(chunk, chunkname, \"t\", env)\n"
"	end\n"
"\n"
"	local pkg = { loaded = {}, preload = {} }\n"
"	S.package = pkg\n"
"	for _, n in ipairs { \"coroutine\", \"math\", \"table\", \"utf8\", \"string\",\n"
"		\"os\", \"package\" } do\n"
"		pkg.loaded[n] = S[n]\n"
"	end\n"
"	pkg.loaded._G = S\n"
"\n"
"	for _, n in ipairs { \"spi\", \"pgtype\", \"elog\", \"error\", \"numeric\",\n"
"		\"jsonb\", \"time\" } do\n"
"		local name = \"pl_lua.\" .. n\n"
"		if allowed[name] == nil then\n"
"			allowed[name] = { module = name, mode = \"direct\" }\n"
"		end\n"
"	end\n"
"	if install_globals then\n"
"		S.spi = loaded[\"pl_lua.spi\"]\n"
"		S.pgtype = loaded[\"pl_lua.pgtype\"]\n"
"	end\n"
"\n"
"	pkg.searchers = {\n"
"		function(name)\n"
"			local p = pkg.preload[name]\n"
"			if p == nil then\n"
"				return \"\\n\\tno field package.preload['\" .. name .. \"']\"\n"
"			end\n"
"			return p, \":preload:\"\n"
"		end,\n"
"		function(name)\n"
"			local e = allowed[name]\n"
"			if e == nil then\n"
"				return \"\\n\\tno allowed module '\" .. name .. \"'\"\n"
"			end\n"
"			return function() return expose(e) end\n"
"		end,\n"
"	}\n"
"\n"
"	S.require = function(name)\n"
"		if type(name) == \"number\" then name = tostring(name) end\n"
"		if type(name) ~= \"string\" then\n"
"			error(\"bad argument #1 to 'require' (string expected, got \" ..\n"
"				type(name) .. \")\", 2)\n"
"		end\n"
"		local v = pkg.loaded[name]\n"
"		if v then return v end\n"
"		local searchers, msgs = pkg.searchers, {}\n"
"		if type(searchers) ~= \"table\" then\n"
"			error(\"'package.searchers' must be a table\", 2)\n"
"		end\n"
"		for i = 1, #searchers do\n"
"			local loader, extra = searchers[i](name)\n"
"			if type(loader) == \"function\" then\n"
"				local r = loader(name, extra)\n"
"				if r ~= nil then pkg.loaded[name] = r end\n"
"				if pkg.loaded[name] == nil then pkg.loaded[name] = true end\n"
"				return pkg.loaded[name], extra\n"
"			elseif type(loader) == \"string\" then\n"
"				msgs[#msgs + 1] = loader\n"
"			end\n"
"		end\n"
"		error(\"module '\" .. name .. \"' not found:\" .. concat(msgs), 2)\n"
"	end\n"
"\n"
"	sandbox = S\n"
"\n"
"	-- apply what was asked for before the sandbox existed\n"
"	for _, e in pairs(allowed) do\n"
"		if e.global then S[e.global] = expose(e) end\n"
"	end\n"
"	for _, r in ipairs(removals) do forget(r[1], r[2]) end\n"
"	removals = {}\n"
"	return S\n"
"end\n"
"\n"
"return M, U\n"
"\n";

/*
 * The pl_lua.trusted module.  The Lua code returns the module and a table
 * of internal functions, which we keep in the registry for
 * pll_sandbox_build().
 */
int
pll_open_trusted(lua_State * L)
{
	if (luaL_loadbuffer(L, pll_trusted_lua, sizeof(pll_trusted_lua) - 1,
						"=pl_lua.trusted") != LUA_OK)
		return lua_error(L);
	lua_getglobal(L, "require");
	lua_call(L, 1, 2);
	lua_rawsetp(L, LUA_REGISTRYINDEX, PLL_REGKEY_TRUSTED_MODS);
	return 1;
}

/*
 * Build the sandbox, after all the modules have been loaded.
 */
void
pll_sandbox_build(lua_State * L, bool install_globals)
{
	lua_rawgetp(L, LUA_REGISTRYINDEX, PLL_REGKEY_TRUSTED_MODS);
	lua_getfield(L, -1, "build");
	lua_pushglobaltable(L);
	lua_getfield(L, LUA_REGISTRYINDEX, LUA_LOADED_TABLE);
	lua_pushboolean(L, install_globals);
	lua_call(L, 3, 1);
	lua_rawsetp(L, LUA_REGISTRYINDEX, PLL_REGKEY_SANDBOX);
	lua_pop(L, 1);
}

/*
 * Remove string.dump from the real string library.  The sandbox's copy never
 * had it; this takes it from code that runs outside the sandbox in a trusted
 * interpreter, too.
 */
void
pll_remove_string_dump(lua_State * L)
{
	lua_getglobal(L, "string");
	if (lua_istable(L, -1))
	{
		lua_pushnil(L);
		lua_setfield(L, -2, "dump");
	}
	lua_pop(L, 1);
}

/* ------------------------------------------------------------------------
 * pl_lua.paths
 * ------------------------------------------------------------------------
 */

static void
pll_bin_path(const char *my_exec_path, char *ret_path)
{
	strlcpy(ret_path, my_exec_path, MAXPGPATH);
	get_parent_directory(ret_path);
}

/* upvalue 1 is a light userdata pointing to the table entry */
static int
pll_path_func(lua_State * L)
{
	const pll_path_entry *entry = lua_touserdata(L, lua_upvalueindex(1));
	char		path[MAXPGPATH];

	entry->fn(my_exec_path, path);
	lua_pushstring(L, path);
	return 1;
}

int
pll_open_paths(lua_State * L)
{
	const pll_path_entry *entry;

	lua_createtable(L, 0, 16);
	for (entry = pll_paths; entry->name != NULL; entry++)
	{
		lua_pushlightuserdata(L, (void *) entry);
		lua_pushcclosure(L, pll_path_func, 1);
		lua_setfield(L, -2, entry->name);
	}
	return 1;
}
