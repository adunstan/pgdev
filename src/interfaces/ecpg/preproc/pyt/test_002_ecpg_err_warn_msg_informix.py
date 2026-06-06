# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test that the ecpg command in INFORMIX mode correctly detects unsupported or
disallowed statements in the input file and reports the appropriate error or
warning messages.
"""

# Input file exercising the warning/error messages in INFORMIX mode.
PGC_SOURCE = """\
/* Test ECPG warning/error messages in INFORMIX mode */

#include <stdlib.h>

int
main(void)
{
    /* For consistency with other tests */
    $CONNECT TO testdb AS con1;

    /* Test AT option usage at CLOSE DATABASE statement in INFORMIX mode */
    $AT con1 CLOSE DATABASE;

    /* Test cursor name errors in INFORMIX mode */
    $DECLARE database CURSOR FOR SELECT * FROM test;

    return 0;
}
"""


def test_002_ecpg_err_warn_msg_informix(pg_bin, tmp_path):
    pgc = tmp_path / "err_warn_msg_informix.pgc"
    pgc.write_text(PGC_SOURCE)

    pg_bin.command_checks_all(
        ["ecpg", "-C", "INFORMIX", str(pgc)],
        3,
        [r""],
        [
            r"ERROR: AT option not allowed in CLOSE DATABASE statement",
            r'ERROR: "database" cannot be used as cursor name in INFORMIX mode',
        ],
        "ecpg in INFORMIX mode with errors and warnings",
    )
