# queue, walltime, nodes, maxconcur: top of pairs.conf; override with e.g. 'make run nodes=8'

# running jobs grep their own *.out.* logs - don't move or delete them underneath
NO_JOBS = @if qstat -u $${USER} 2>/dev/null | grep -q cgd_; then \
	echo "cgd_ jobs are queued or running on this cluster - refusing (they read these logs)"; exit 1; fi

list:
	@bash -c '. ./pairs.sh && pairs_list'

# done stamps & latest job log line per pair
status:
	@bash -c '. ./pairs.sh && pairs_status'

# counts, plus only the pairs not yet done - e.g. 'pairs_conf=pairs.3A-CESM-HR.split.conf make status_brief'
status_brief:
	@bash -c '. ./pairs.sh && pairs_status brief'

# e.g. make run PAIRS="3C-cmip6-timeseries 3D-cmip5-timeseries" rounds=2 [fresh_after=now]
run: submit_sync_array.sh sync_dir_job_array.sh pairs.conf msrsync
	./submit_sync_array.sh $(PAIRS)

link:
	./link_cold_storage.sh

# root hand-off scripts: rsync flavour for pairs keeping source ownership, chown for 'owner:' pairs
root_commands:
	./make_root_commands.sh

root_chown_commands:
	./make_root_commands.sh --chown

# before the copy: read ACLs on the sources, one setfacl per pair, e.g. 'make root_acl_commands PAIRS=...'
root_acl_commands:
	./make_root_commands.sh --acl $(PAIRS)

# request GPFS compression of the copies now, e.g. 'make compress PAIRS=3D-cmip5-timeseries'
# (deferred to the next policy run; ./compress_dest.sh --now to compress immediately)
compress:
	./compress_dest.sh $(PAIRS)

compress_status:
	./compress_dest.sh --status $(PAIRS)

# keeps the flist-destdir.*.xz indexes and the walksync.<pair>.<job>.<time>/ directories
# (walksync's summary and its per-rank indexes of what it copied)
clean:
	$(NO_JOBS)
	find . -maxdepth 1 -type f \( -name '*~' -o -name '*.out.*' -o -name '*.err.*' -o -name 'cgd_*.o*' \
	    -o -name 'mpibind.*.log' -o -name 'verify_*' \) ! -name '*.xz' -delete

archive_logs:
	$(NO_JOBS)
	timestamp=$$(date +%F@%H:%M) ; \
	mkdir -p logs/$${timestamp} ; \
	mv -f cgd_*.o* *.out.* *.err.* mpibind.*.log verify_* logs/$${timestamp} ; \
	for d in walksync.*/ ; do if [ -d "$$d" ]; then mv -f "$$d" logs/$${timestamp}/ ; fi ; done

# read-only check of a pair's dest before deleting it, a 1-node PBS job: walkstat --index=gz of dest,
# then entries not ours, dirs without u+w, and atimes/mtimes after SINCE (default: the done stamp's
# 'finished'); a pass writes preflight.<pair>.ok, which delete_dest requires. AFTER=<jobid> waits for it.
#   make preflight PAIR=4D-CESM2-LE [SINCE='2026-11-06 18:00'] [AFTER=8155365]
preflight:
	@test -n "$(PAIR)" || { echo "usage: make preflight PAIR=<pair> [SINCE='YYYY-MM-DD HH:MM'] [AFTER=<jobid>]"; exit 1; }
	@bash -c '. ./pairs.sh && pair_load "$$1" && export since="$$2" && submit_pair_job dest_preflight.sh 1 "$$3" since' \
	    _ "$(PAIR)" "$(SINCE)" "$(AFTER)"

# remove a pair's dest with walkrm, a PBS job (NODES=2 by default). Refused unless CONFIRM is exactly the
# pair's dest, it is a real directory under allowed_roots, no sync holds the pair's lock, and a passing
# preflight (above, under 7 days) exists; PREFLIGHT=skip overrides only the last. DRYRUN=yes removes nothing.
# On success the pair's done stamp moves aside, so a later 'make run' copies it again.
#   make delete_dest PAIR=4D-CESM2-LE CONFIRM=/glade/campaign/cesm/cross-wg/CESM2-LE [AFTER=8155365]
delete_dest:
	@test -n "$(PAIR)" || { echo "usage: make delete_dest PAIR=<pair> CONFIRM=<dest> [PREFLIGHT=skip] [DRYRUN=yes] [AFTER=<jobid>] [NODES=2]"; exit 1; }
	@bash -c '. ./pairs.sh && pair_load "$$1" && delete_dest_check "$$2" "$$3" \
	    && export confirm="$$2" preflight="$$3" dryrun_rm="$${5:-no}" \
	    && submit_pair_job delete_dest.sh "$${6:-2}" "$$4" confirm preflight dryrun_rm' \
	    _ "$(PAIR)" "$(CONFIRM)" "$(PREFLIGHT)" "$(AFTER)" "$(DRYRUN)" "$(NODES)"

# this cluster's cgd_ jobs (derecho or casper)
qdelall:
	qdel $$(qstat -u $${USER} 2>/dev/null | grep "cgd_" | cut -d'.' -f1)

.PHONY: preflight delete_dest list status status_brief run link root_commands root_chown_commands root_acl_commands compress compress_status clean archive_logs qdelall
