queue ?= main
walltime ?= 12:00:00

echo:
	@echo "Hi"

clean:
	rm -f *~ *.out.* *.err.* scan_*.o*

clobber:
	$(MAKE) clean

archive_logs:
	timestamp=$$(date +%F@%H:%M) ; \
	mkdir -p logs/$${timestamp}/ ; \
	mv -f *.xlsx *_*.o* *.out.* *.err.* logs/$${timestamp}/

qdelall:
	qdel $$(qstat -w -u $${USER} 2>/dev/null | grep ".desch" | egrep "dsync|dwalk|mirror" | cut -d'.' -f1)


run: submit_scan_array.sh scan_dir_job_array.sh scan_dirs.cfg
	./$<
