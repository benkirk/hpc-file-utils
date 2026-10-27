# Peer review: filesystem/cgd_migration (branch cgd_migration vs main)

## Context
You asked for a review only: no edits, commits or job submissions. I read the README, all six shell
scripts, split_pair.py, the Makefile and pairs.conf (including the uncommitted `batch_files: 25000` on 3B),
and diffed msrsync against `../p_migration/msrsync.BSK`. The only difference is the `MSRSYNC_PRUNE` hunk.
Shellcheck at warning level is clean; at info level it only flags unquoted `${suffix}`/`${sstart}`,
which is harmless here. Nothing was run on /glade. "PLAUSIBLE" means I could not confirm a tool's
behaviour offline, and the finding says what to check.

## Findings, most severe first

### 1. data-loss: nothing limits what `rsync --delete` may remove
`sync_dir_job_array.sh:300`, `pairs.sh:64-87`
- **How it fails:** the only check on src is `[[ -d ${srcdir} ]]`. If src is present but empty or wrong, dfind finds nothing and dsync copies nothing. The serial `rsync --delete` then empties the dest copy, exits 0, and the pair gets a done stamp. Ways src can end up empty or wrong:
  - a fileset junction unlinked or relinked on downtime day;
  - a `pairs.conf` typo that points at an empty directory;
  - a split subtree that has been emptied since the scan.
- **The same happens when:**
  - dest already holds unrelated data (rsync makes dest match src, so it deletes that data);
  - src is inside dest (e.g. `dest: /glade/campaign/cesm`, `src: .../cesm/x`): `--delete` removes `dest/x`, which is the source itself.
- No check requires paths to be absolute, unequal, or not nested inside each other.
- **Fix:**
  - add `--max-delete=N` to the serial rsync. On overflow rsync exits 25, which already means "no stamp". Pick a per-pair N, e.g. 1% of dfind's "Walked N items" or a fixed 10000.
  - In `pair_load`, require absolute paths under `/glade/campaign/`, src ≠ dest, and neither path a prefix of the other (compare `realpath` with a trailing `/`).
  - Before the first stage, refuse a src with no entries.

### 2. wrong-result (PLAUSIBLE): real dsync errors are hidden, so a corrupt copy can be stamped
`sync_dir_job_array.sh:236-244, 300`
- As non-root, dsync always exits non-zero because of lchown EPERM. The script therefore ignores its exit code and checks only for `Completed sync`.
- A real write error looks the same: ENOSPC or EDQUOT (cesm/cmip quota is tight, sources expand about 1.65x), EIO, or a lost rank.
- If dsync still sets size and mtime on a file it failed to write (it truncates to final size, then sets metadata per batch), rsync's size+mtime quick check skips that file. The stamp then certifies a file with holes. rsync never compares contents, so it is not the "independent check" the README calls it.
- **Check:** whether dsync 0.12 sets metadata on files whose writes failed (try it on a small quota'd test fileset).
- **Fix:**
  - treat any `dsync.err` line that is not the known lchown/EPERM message as a failure (allow-list that one pattern), and match `No space|quota|Input/output|ERROR` explicitly;
  - add a free-space/quota preflight (`df` on the dest fileset vs. the source's true size);
  - before writing the stamp, `cmp` a random sample of large files (e.g. 200).

### 3. data-safety (tape recall): the COLD_STORAGE guard can see zero matches when there are some
`sync_dir_job_array.sh:181`
- **How it fails:** `grep '^/'` runs in the job's UTF-8 locale. These trees are known to contain non-UTF-8 names (that is why msrsync.BSK exists). When GNU grep meets badly encoded input it treats it as binary, prints a "binary file matches" notice instead of the lines, and stops printing matches.
  - If the first COLD_STORAGE path dfind prints has a bad byte, `ncs=0`, the guard passes and dsync reads the stubs.
  - The same happens if a launcher ever prefixes lines with rank labels (`0: /glade/...`).
  - The `Walked ...` check proves dfind ran; it does not prove the matches were parsed.
- **Also:** for hsm pairs the shortened `cold_storage.<pair>.list` makes `make link` silently skip links.
- **Fix:** use `LC_ALL=C grep -a '^/'` and `LC_ALL=C sed/sort`. Add an independent count that fails closed: if `LC_ALL=C grep -ac COLD_STORAGE dfind.out.*` is greater than 0 and the pair is not `hsm: yes`, abort, whatever `ncs` says. Also check `rcs[4]` (sort/redirect): if the list cannot be written, the previous run's list stays in place.

### 4. data-safety (assumption): stubs are recognised by name only
`sync_dir_job_array.sh:51,174-198`, `split_pair.py:75`
- The guard catches only migrated files whose path contains a directory or name matching `COLD_STORAGE*` (case-sensitive). It misses:
  - a hard link outside COLD_STORAGE to a migrated inode (every name of that inode is a stub);
  - a case variant such as `Cold_Storage`;
  - a file migrated by any other policy rule.
- **Fix (one time, cheap, stat only):** before the downtime, run the existing stub check from `../glade_hsm/libexec/glade_hsm_scan_filelist.py:82-96` (size > 100 MiB and > 100x the on-disk bytes) over every non-hsm source. For example `find <src> -type f -size +100M -print0 | glade_hsm_scan_filelist.py` and look for `Offline` > 0. Or use `find -printf '%s %b %p\n'` with the same ratio. Compressed files will not trip a 100x ratio.

### 5. wrong-result (operator): a stamp never expires, and `make status` can disagree with the jobs
`pairs.sh:129-134,157-161`, `submit_sync_array.sh:31`, README runbook step 2
- `fresh_after` given in the environment is resolved once at submission and passed only to the jobs.
- A plain `make status` (no env) reads `pairs.conf`, which has no `fresh_after:`. It therefore shows pre-read-only stamps as **valid** while the final pass is still running or has not run. The runbook says to repeat "until `make status` shows every pair valid", so this display is exactly what the operator acts on.
- Forgetting `fresh_after` entirely means every stamped pair exits in seconds and the final pass never happens.
- **Fix:** submission writes the resolved epoch to `stamps/.fresh_after`, and both `stamp_check` callers use the larger of that value, the conf value and the env value. Or drop the env form and make `fresh_after:` in `pairs.conf` the only mechanism on downtime day. `make status` should print a warning when it is unset.

### 6. wrong-result: an array index is mapped to a pair from `pairs.conf` when the job runs, not when it is submitted
`sync_dir_job_array.sh:55-62`, `submit_sync_array.sh:48-50`
- `make run` with no `PAIRS` sends no `pair_list`, so member *i* runs whichever section is *i*-th when it **starts**. Inserting or reordering a section while rounds are queued shifts every member: the job runs a pair nobody submitted, and the last pair is never run.
- src, dest and tunables are also re-read when the job starts, for every pair. That is useful for `batch_files` but dangerous for src/dest edits.
- Your live arrays use `pair_list`, so they are safe.
- **Fix:** always pass `pair_list`, including the all-pairs case. Optionally, copy the conf at submission (`stamps/conf.<ts>`) and pass that as `pairs_conf`.

### 7. operational: a stale lock stops a pair's whole chain
`sync_dir_job_array.sh:108-112`
- The `TERM` trap runs only after the foreground child exits. If mpibind/PALS teardown takes longer than PBS's kill_delay, SIGKILL leaves the lock in place. A node failure followed by a `-r y` requeue always does: there is no trap at all on the dead mother superior.
- Walltime kills during dsync are expected (3C). After a stale lock, every later `afterany` round exits 1 at `lockfile` within about 40 s, and the chain makes no progress without any visible sign.
- **Fix:** make the lock an atomic `ln -s "${PBS_JOBID}" "${LOCK}"`. When it fails, `readlink` the lock and `qstat` that job id; take over the lock if the job is no longer running. This also makes `-r y` requeues work.

### 8. operational: `make archive_logs` / `make clean` break running jobs
`Makefile:28-34`
- Both act on `*.out.*`, which covers the `dfind.out.*` and `dsync.out.*` files a running job is about to grep. Moving or deleting them makes the job abort ("dfind failed" / "dsync did not complete") after hours of dsync, or writes a stamp with empty rsync counts.
- `clean` also deletes the `flist-destdir.out.*.xz` indexes.
- **Fix:** refuse while `qstat -u $USER` shows `cgd_` jobs, or move only files whose job id has finished. Leave `*.xz` out of `clean`.

### 9. wrong-result (policy): the root hand-off script is a recursive chown, and it is unquoted
`Makefile:17-18` → `make_root_commands.sh:29`
- The hand-off itself is the right approach: `make root_commands` writes `root_commands.<date>.sh` for the root group. The problem is what it writes: one `chown -R -h -P ${owner} ${destdir}` per pair. That is the recursive chown, and it gives every file in dest a single uid:gid.
- If a source has mixed owners or groups, that is lost.
- As non-root, dsync (EPERM on lchown) and rsync (`-o` ignored, `-g` only for groups you belong to) also leave owner, group and setgid bits wrong. A blanket chown does not restore those either.
- Paths are written without quoting.
- **Check:** `find <src> -name 'COLD_STORAGE*' -prune -o -printf '%u:%g\n' | sort | uniq -c`.
- **Fix: two hand-off flavours, both generated scripts that only the root group runs.** Each pair goes into exactly one of them:
  - `make root_commands` → `root_commands.<date>.sh`, for pairs **without** `owner:`. Here ownership is assumed to be the same as the source's. Emit `rsync -aHAX --numeric-ids --exclude='COLD_STORAGE*' <src>/ <dest>/` with no `--delete`. As root it copies source owner, group and mode bits onto files already in place, without copying data. The exclude keeps it out of the HSM directories and leaves the dest symlinks alone.
  - `make root_chown_commands` → `root_chown_commands.<date>.sh`, only for pairs **with** `owner:` (a new owner:group requested in the config). Emit `chown -R -h -P <owner> <dest>` (the current behaviour).
  - Each script lists the pairs it skipped and why, so no pair goes unhandled without anyone noticing.
  - Quote every path and owner with `printf '%q'`.
  - Note: the chown flavour does not restore setgid or mode bits that the non-root copy dropped. If those matter for an owner pair, it would need a chmod pass as well.

### 10. operational/throughput: the split can still send large trees through single-node msrsync
`split_pair.py:143`, `sync_dir_job_array.sh:265-268`
- `or total_size_nr >= min_size` makes a mixed subtree an hsm pair whenever it has 1 TiB or more of loose files of its own, however big its clean children are. A 500 TiB subtree can end up in one msrsync job.
- The final whole-tree 3A pass is `hsm: yes`, so `msrsync: auto` re-crawls all of CESM-HR (about 1.6 PiB) on one node before the serial rsync. Both must finish in one walltime to get a stamp.
- **Fix:** only `size <= max_hsm` should become an hsm pair; otherwise descend and leave the loose files to the final pass. Set `msrsync: no` on `[3A-CESM-HR]` once the split has done the bulk copy.
- **Resolution (2026-10-25): N/A for 3A, rule kept.** The 3A split's largest hsm pairs are five `BRCP45/.../atm/proc/tseries/hour_6` dirs, 65-74 TiB each, almost all loose files next to one `COLD_STORAGE` subdir and with no clean children. The fix would only move those ~345 TiB into the single final pass (4.4 TiB -> 349 TiB). The split header now names the largest hsm pair. `msrsync: auto` stays on `[3A-CESM-HR]`: after the split, it parallelizes the ~4.4 TiB of leftovers.

### 11. operational: split children and the parent pass are not kept apart
- The parent and the children use different locks. If they overlap, the parent's `--delete` removes the children's in-flight rsync temp files, which makes those children exit 23. The copies themselves are not damaged; it only causes extra work.
- The README's `after='NNN[]'` handles this, but the split_pair.py docstring (line 21) says `after=NNN`. Fix the docstring.
- Optional: the parent pass could refuse to start while a `<pair>.NNN` lock exists.

### 12. low data-safety (PLAUSIBLE): `MSRSYNC_PRUNE` prunes directories only
`msrsync:553`
- A non-directory named `COLD_STORAGE*` still goes into a `--files-from` bucket. I could not confirm offline that rsync applies `--exclude` to `--files-from` entries.
- **Fix (one line):** also filter `files` with the same patterns.

### 13. low: config and name validation
`pairs.sh:43,76-77`
- The trailing-comment strip cuts a path at the first " #" (e.g. `run #2`), so a split could produce a path that silently names a different directory.
- Only one trailing `/` is stripped.
- Duplicate section names are accepted.
- Section names are not limited to `[A-Za-z0-9._-]`; a `/` in a name breaks the log and stamp paths.
- **Fix:** validate all of these in `pair_load` / `pairs_list`.

### 14. cosmetic
- dryrun mode runs `mkdir -p "${destdir}"` (line 136) before it exits.
- dryrun passes `--delete` to dsync, but the real dsync run does not.
- `jobid` drops the brackets, so `1234[5]` and job `12345` give the same suffix (line 131).
- `2>&1` captures xz's stderr, not find's (line 333).
- `src_real` is used unescaped in the sed regex (line 181).
- `grep -v "${exe_host}"` is a substring/regex match; use `grep -vxF` on short names (line 253).
- `qdelall` matches only `.desch` (derecho) and uses `egrep`.
- `[[ -ge ]]` evaluates stamp content as arithmetic, so an empty `started:` counts as 0.
- The index stage also runs after a failed rsync.
- No-op rounds still wait in the queue for N full nodes. The submit script could leave out pairs already stamped valid from round 1.

## Areas with nothing serious
- **Trailing slashes / excludes / symlinks:** handled consistently. `src/ dest/` is used everywhere. The `COLD_STORAGE*` exclude both stops the sender from walking stub directories and protects the dest symlinks from `--delete`. dfind, dsync (`--no-dereference`), rsync `-a`, `find` (index, compress), `chown -h -P` and msrsync's `os.walk` never follow links. dfind and msrsync only stat stubs, which triggers no recall.
- **Split nesting:** split_pair emits leaves only, so no split dest is inside another split dest. Children are subsets of the parent, so the parent's `--delete` never removes their copies.
- **Done semantics:** a stamp requires a clean serial rsync. A walltime kill, exit 23 or exit 24 leaves no stamp. Writing `.tmp` then `mv` is atomic. Comparing src/dest catches renumbered splits. Judging freshness by when the pass **started** is the right, conservative choice. Clock skew and the Nov 1 DST change do not matter (epoch from `date -d`).
- **Arrays sharing a run directory:** per-pair and per-jobid file names, the stamp written via `.tmp`, and the cs_list written under the lock mean members do not collide. `afterany` on `NNN[]` chaining is correct.
- **Shell robustness:** PIPESTATUS is captured immediately and indexed correctly. `pairs.sh` does `return || exit` correctly whether sourced or executed. Arrays are quoted. Shellcheck is clean at warning level.
- **Simplicity:** proportionate to the job. The stamp/rounds ratchet, `msrsync: auto` and the split are each justified. What is missing for about 4 PiB is the safety rails in #1, #2 and #3 (`--max-delete`, a dsync error allow-list and quota preflight, a sampled content check), not more machinery.

## Suggested order if you want fixes later (not done here)
1. #1, #3 and #2: the `--max-delete` and path checks, the LC_ALL/fail-closed count, and the dsync error allow-list. Small diffs in `sync_dir_job_array.sh` and `pairs.sh`.
2. #5, #6 and #7, before downtime day.
3. #4 and #9's ownership check: one-time read-only scans you can run now.

Verification for those fixes: shellcheck; a smoke pair in `/glade/derecho/scratch/benkirk/cgd_smoke` with
- an empty src (expect rsync exit 25 and no stamp);
- a non-UTF-8-named `COLD_STORAGE` directory in a non-hsm pair (expect an abort);
- a `kill -9`'d job (expect the next round to take over the lock).
