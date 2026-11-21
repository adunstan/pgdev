#-------------------------------------------------------------------------
#
# Makefile for the PL/Lua procedural language
#
# src/pl/pl_lua/Makefile
#
#-------------------------------------------------------------------------

subdir = src/pl/pl_lua
top_builddir = ../../..
include $(top_builddir)/src/Makefile.global

override CPPFLAGS := -I. -I$(srcdir) $(CPPFLAGS) $(LUA_CFLAGS)

# Lua is linked into the shared library; transform modules resolve the Lua
# symbols from it.
SHLIB_LINK = $(LUA_LIBS)

PGFILEDESC = "PL/Lua - procedural language"

NAME = pl_lua

OBJS = \
	$(WIN32RES) \
	pl_lua.o \
	pl_lua_datum.o \
	pl_lua_error.o \
	pl_lua_jsonb.o \
	pl_lua_numeric.o \
	pl_lua_spi.o \
	pl_lua_time.o \
	pl_lua_trigger.o \
	pl_lua_trusted.o

DATA = pl_lua.control pl_lua--1.0.sql \
       pl_luau.control pl_luau--1.0.sql

HEADERS = pl_lua.h

TAP_TESTS = 1

REGRESS_OPTS = --dbname=$(PL_TESTDB)
REGRESS = basic \
	srf \
	strict \
	interpreters \
	errors \
	sandbox \
	sandbox_env \
	sandbox_inventory \
	trusted \
	paths \
	encoding \
	subxact \
	types \
	arrays \
	rowdatum \
	numerics \
	jsonb \
	horology \
	horology_errors \
	spi \
	cursors \
	quote \
	helpers \
	procedures \
	triggers \
	event_triggers \
	roles \
	privileges \
	attacks \
	extension

include $(top_srcdir)/src/Makefile.shlib

all: all-lib

# Force this dependency to be known even without dependency info built:
pl_lua_error.o: plerrcodes.h

# generate plerrcodes.h from src/backend/utils/errcodes.txt, using the
# generator that PL/pgSQL uses
plerrcodes.h: $(top_srcdir)/src/backend/utils/errcodes.txt $(top_srcdir)/src/pl/plpgsql/src/generate-plerrcodes.pl
	$(PERL) $(top_srcdir)/src/pl/plpgsql/src/generate-plerrcodes.pl $< > $@

install: all install-lib install-data install-headers

installdirs: installdirs-lib
	$(MKDIR_P) '$(DESTDIR)$(datadir)/extension' '$(DESTDIR)$(includedir_server)'

uninstall: uninstall-lib uninstall-data uninstall-headers

install-data: installdirs
	$(INSTALL_DATA) $(addprefix $(srcdir)/, $(DATA)) '$(DESTDIR)$(datadir)/extension/'

uninstall-data:
	rm -f $(addprefix '$(DESTDIR)$(datadir)/extension'/, $(notdir $(DATA)))

install-headers: installdirs
	$(INSTALL_DATA) $(addprefix $(srcdir)/, $(HEADERS)) '$(DESTDIR)$(includedir_server)/'

uninstall-headers:
	rm -f $(addprefix '$(DESTDIR)$(includedir_server)'/, $(HEADERS))

.PHONY: install-data uninstall-data install-headers uninstall-headers

check: submake-pg-regress
	$(pg_regress_check) $(REGRESS_OPTS) $(REGRESS)
	$(prove_check)

installcheck: submake-pg-regress
	$(pg_regress_installcheck) $(REGRESS_OPTS) $(REGRESS)
	$(prove_installcheck)

.PHONY: submake-pg-regress
submake-pg-regress: | submake-generated-headers
	$(MAKE) -C $(top_builddir)/src/test/regress pg_regress$(X)

clean distclean: clean-lib
	rm -f $(OBJS)
	rm -f plerrcodes.h
	rm -rf $(pg_regress_clean_files) tmp_check
