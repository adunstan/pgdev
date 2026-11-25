# contrib/hstore_pl_lua/Makefile

MODULE_big = hstore_pl_lua
OBJS = \
	$(WIN32RES) \
	hstore_pl_lua.o
PGFILEDESC = "hstore_pl_lua - hstore transform for PL/Lua"

EXTENSION = hstore_pl_lua hstore_pl_luau
DATA = hstore_pl_lua--1.0.sql hstore_pl_luau--1.0.sql

REGRESS = create_ext hstore hstore_luau
EXTRA_INSTALL = contrib/hstore src/pl/pl_lua

ifdef USE_PGXS
PG_CPPFLAGS = -I$(includedir_server)/extension
PG_CONFIG = pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
else
PG_CPPFLAGS = -I$(top_srcdir)/src/pl/pl_lua -I$(top_srcdir)/contrib
subdir = contrib/hstore_pl_lua
top_builddir = ../..
include $(top_builddir)/src/Makefile.global
include $(top_srcdir)/contrib/contrib-global.mk
endif

# Compile against the Lua headers, but do not link Lua: the symbols are
# resolved at load time from pl_lua, which uses its own copy.
override CPPFLAGS := $(CPPFLAGS) $(LUA_CFLAGS)
