# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Test checking options of pg_rewind."""


def test_006_options(pg_bin, tmp_path):
    pg_bin.program_help_ok("pg_rewind")
    pg_bin.program_version_ok("pg_rewind")
    pg_bin.program_options_handling_ok("pg_rewind")

    primary_pgdata = str(tmp_path / "primary")
    standby_pgdata = str(tmp_path / "standby")

    pg_bin.command_fails(
        [
            "pg_rewind",
            "--debug",
            "--target-pgdata", primary_pgdata,
            "--source-pgdata", standby_pgdata,
            "extra_arg1",
        ],
        "too many arguments",
    )
    pg_bin.command_fails(
        ["pg_rewind", "--target-pgdata", primary_pgdata],
        "no source specified",
    )
    pg_bin.command_fails(
        [
            "pg_rewind",
            "--debug",
            "--target-pgdata", primary_pgdata,
            "--source-pgdata", standby_pgdata,
            "--source-server", "incorrect_source",
        ],
        "both remote and local sources specified",
    )
    pg_bin.command_fails(
        [
            "pg_rewind",
            "--debug",
            "--target-pgdata", primary_pgdata,
            "--source-pgdata", standby_pgdata,
            "--write-recovery-conf",
        ],
        "no local source with --write-recovery-conf",
    )
