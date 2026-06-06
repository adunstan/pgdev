# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Run the ecpg preprocessor on an input file and check that it detects
unsupported or disallowed statements and reports the appropriate error or
warning messages.
"""

# Input file exercising the warning/error messages.
ERR_WARN_MSG_PGC = """\
/* Test ECPG warning/error messages */

#include <stdlib.h>

int
main(void)
{
\tEXEC SQL BEGIN DECLARE SECTION;
\tchar *cursor_var = "mycursor";
\tshort a;
\tEXEC SQL END DECLARE SECTION;

\t/* For consistency with other tests */
\tEXEC SQL CONNECT TO testdb AS con1;

\t/* Test AT option errors */
\tEXEC SQL AT con1 CONNECT TO testdb2;
\tEXEC SQL AT con1 DISCONNECT;
\tEXEC SQL AT con1 SET CONNECTION TO testdb2;
\tEXEC SQL AT con1 TYPE string IS char[11];
\tEXEC SQL AT con1 WHENEVER NOT FOUND CONTINUE;
\tEXEC SQL AT con1 VAR a IS int;

\t/* Test COPY FROM STDIN warning */
\tEXEC SQL COPY test FROM stdin;

\t/* Test same variable in multi declare statement */
\tEXEC SQL DECLARE :cursor_var CURSOR FOR SELECT * FROM test;
\tEXEC SQL DECLARE :cursor_var CURSOR FOR SELECT * FROM test;

\t/* Test duplicate cursor declarations */
\tEXEC SQL DECLARE duplicate_cursor CURSOR FOR SELECT * FROM test;
\tEXEC SQL DECLARE duplicate_cursor CURSOR FOR SELECT * FROM test;

\t/* Test SHOW ALL error */
\tEXEC SQL SHOW ALL;

\t/* Test deprecated LIMIT syntax warning */
\tEXEC SQL SELECT * FROM test LIMIT 10, 5;

\treturn 0;
}
"""


def test_001_ecpg_err_warn_msg(pg_bin, tmp_path):
    pg_bin.program_help_ok("ecpg")
    pg_bin.program_version_ok("ecpg")
    pg_bin.program_options_handling_ok("ecpg")
    pg_bin.command_fails(["ecpg"], "ecpg without arguments fails")

    # Test that the ecpg command correctly detects unsupported or disallowed
    # statements in the input file and reports the appropriate error or
    # warning messages.
    pgc = tmp_path / "err_warn_msg.pgc"
    pgc.write_text(ERR_WARN_MSG_PGC)

    pg_bin.command_checks_all(
        ["ecpg", str(pgc)],
        3,
        [r""],
        [
            r"ERROR: AT option not allowed in CONNECT statement",
            r"ERROR: AT option not allowed in DISCONNECT statement",
            r"ERROR: AT option not allowed in SET CONNECTION statement",
            r"ERROR: AT option not allowed in TYPE statement",
            r"ERROR: AT option not allowed in WHENEVER statement",
            r"ERROR: AT option not allowed in VAR statement",
            r"WARNING: COPY FROM STDIN is not implemented",
            r'ERROR: using variable "cursor_var" in different declare statements is not supported',
            r'ERROR: cursor "duplicate_cursor" is already defined',
            r"ERROR: SHOW ALL is not implemented",
            r"WARNING: no longer supported LIMIT",
            r'WARNING: cursor "duplicate_cursor" has been declared but not opened',
            r'WARNING: cursor "duplicate_cursor" has been declared but not opened',
            r'WARNING: cursor ":cursor_var" has been declared but not opened',
            r'WARNING: cursor ":cursor_var" has been declared but not opened',
        ],
        "ecpg with errors and warnings",
    )
