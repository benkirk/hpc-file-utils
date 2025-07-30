queue ?= main
walltime ?= 12:00:00

echo:
	@echo "Hi"

clean:
	rm -f *~ *.out *.err *.sh.o* scan_dirs.o*.*

clobber:
	$(MAKE) clean
	rm -f *.DONE summary-*
	rm -rf usercounts

archive_logs:
	timestamp=$$(date +%F@%H:%M) ; \
	mkdir -p logs/$${timestamp}/ ; \
	mv -f summary-* usercounts logs/$${timestamp}/ ; \
	rm -f *.DONE *.out *.err scan_dirs.o*.* logs/latest ; \
	cd logs; \
	ln -s $${timestamp} latest

qdelall:
	qdel $$(qstat -w -u $${USER} 2>/dev/null | grep ".desch" | egrep "scan_" | cut -d'.' -f1)


run: submit_scan_array.sh scan_dir_job_array.sh scan_dirs.cfg
	./$<
