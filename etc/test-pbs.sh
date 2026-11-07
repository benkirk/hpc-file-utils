#!/bin/bash -l
#PBS -N hfu-test
#PBS -j oe
#PBS -r n
#PBS -l select=1:ncpus=128:mpiprocs=128:mem=235G
#PBS -l walltime=00:30:00

# 'make test-pbs' submits this (account, queue and walltime on its qsub line):
# the mpi and site tests, which need a PBS job on derecho (cray-mpich only launches
# inside one). The env was built on the login node by the same make target.

cd "${PBS_O_WORKDIR}" || exit 1
# shellcheck source=etc/config_env.sh
source etc/config_env.sh || exit 1

echo "job ${PBS_JOBID} on $(hostname), NCAR_HOST=${NCAR_HOST:-}, HFU_MPI=${HFU_MPI:-auto}"
module list 2>&1
which python mpiexec
python -c "from mpi4py import MPI; print(MPI.Get_library_version().splitlines()[0])"

# shellcheck disable=SC2086  # PYTEST_ARGS: extra arguments, word-split on purpose
python -m pytest -m "mpi or site" -n 16 -rxXs ${PYTEST_ARGS:-}
