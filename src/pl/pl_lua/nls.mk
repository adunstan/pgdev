# src/pl/pl_lua/nls.mk
CATALOG_NAME     = pl_lua
GETTEXT_FILES    = pl_lua.c pl_lua_datum.c pl_lua_error.c pl_lua_jsonb.c \
                   pl_lua_numeric.c pl_lua_spi.c pl_lua_time.c \
                   pl_lua_trigger.c pl_lua_trusted.c
GETTEXT_TRIGGERS = $(BACKEND_COMMON_GETTEXT_TRIGGERS)
GETTEXT_FLAGS    = $(BACKEND_COMMON_GETTEXT_FLAGS)
