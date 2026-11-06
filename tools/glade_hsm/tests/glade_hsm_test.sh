#!/usr/bin/env bash
# pytest-mark: site
#
# Runs the original ../test.sh, which migrates ~2 GiB of test files under
# /glade/campaign/cisl/csg/${USER}-tmp-hsm_tests and prompts on /dev/tty.
# So it runs only on GLADE, when asked: HFU_SITE_WRITE_TESTS=1, with a terminal
# (e.g. 'HFU_SITE_WRITE_TESTS=1 pytest -n 0 -s tools/glade_hsm/tests').

[[ -d /glade/campaign ]] || { echo "SKIP: /glade/campaign is not mounted"; exit 77; }
[[ ${HFU_SITE_WRITE_TESTS:-} == 1 ]] \
    || { echo "SKIP: writes to campaign storage; set HFU_SITE_WRITE_TESTS=1 to run"; exit 77; }
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/test.sh"
