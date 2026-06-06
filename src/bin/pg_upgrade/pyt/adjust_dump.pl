#!/usr/bin/perl

# Copyright (c) 2024-2026, PostgreSQL Global Development Group

# Thin CLI wrapper around PostgreSQL::Test::AdjustDump and
# PostgreSQL::Test::AdjustUpgrade, so the Python port of
# bin/pg_upgrade/t/002_pg_upgrade.pl can reuse the exact dump-adjustment logic
# (which is version-conditional and substantial) instead of reimplementing it.
#
# Reads a dump from stdin, writes the adjusted dump to stdout.  Usage:
#
#   adjust_dump.pl regress <0|1>      # adjust_regress_dumpfile($dump, $adjust_child_columns)
#   adjust_dump.pl old <old_version>  # adjust_old_dumpfile($old_version, $dump)
#   adjust_dump.pl new <old_version>  # adjust_new_dumpfile($old_version, $dump)
#
# Run with the in-tree Perl test modules on the include path, e.g.
#   perl -I src/test/perl adjust_dump.pl ...

use strict;
use warnings FATAL => 'all';

use PostgreSQL::Version;
use PostgreSQL::Test::AdjustDump;
use PostgreSQL::Test::AdjustUpgrade;

my $mode = shift @ARGV;
die "usage: adjust_dump.pl <regress|old|new> <arg>\n"
  unless defined $mode;

# Slurp the entire dump from stdin.
local $/;
my $dump = <STDIN>;
$dump = '' unless defined $dump;

my $out;
if ($mode eq 'regress')
{
	my $adjust_child_columns = shift @ARGV;
	$adjust_child_columns = 0 unless defined $adjust_child_columns;
	$out = adjust_regress_dumpfile($dump, $adjust_child_columns);
}
elsif ($mode eq 'old')
{
	my $old_version = PostgreSQL::Version->new(shift @ARGV);
	$out = adjust_old_dumpfile($old_version, $dump);
}
elsif ($mode eq 'new')
{
	my $old_version = PostgreSQL::Version->new(shift @ARGV);
	$out = adjust_new_dumpfile($old_version, $dump);
}
else
{
	die "unknown mode \"$mode\"\n";
}

binmode STDOUT;
print $out;
