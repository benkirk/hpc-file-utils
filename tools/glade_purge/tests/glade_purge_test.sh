#!/usr/bin/env bash
# pytest-mark: site
#
# Runs the original ../test.sh, which creates and purges test trees in ${HOME},
# ${WORK}, ${SCRATCH} and /glade/campaign/cisl/csg/${USER}, and leaves PURGE dirs
# behind on purpose (its test 5). So it runs only on GLADE, when asked:
# HFU_SITE_WRITE_TESTS=1 (e.g. 'HFU_SITE_WRITE_TESTS=1 pytest -n 0 tools/glade_purge/tests').

[[ -d /glade/campaign ]] || { echo "SKIP: /glade/campaign is not mounted"; exit 77; }
[[ -n ${WORK:-} && -n ${SCRATCH:-} ]] || { echo "SKIP: needs \$WORK and \$SCRATCH"; exit 77; }
[[ ${HFU_SITE_WRITE_TESTS:-} == 1 ]] \
    || { echo "SKIP: writes to HOME, WORK, SCRATCH and campaign storage; set HFU_SITE_WRITE_TESTS=1 to run"; exit 77; }
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/test.sh"
