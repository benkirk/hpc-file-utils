all: .testdirstamp

.testdirstamp: Makefile
	mkdir -p testtree.tmp/{sample1,sample2,foo,bar,COLD_STORAGE/baz,COLD_STORAGE/bar,COLD_STORAGE_SINGLE_COPY/foo}
	for dir in $$(find testtree.tmp -type d); do \
	  for cnt in $$(seq 0 9); do \
	    date > $${dir}/date-$${cnt}.txt ; \
	  done ; \
	  for cnt in $$(seq 0 3); do \
	    dd if=/dev/zero of=$${dir}/dd-$${cnt}.dat bs=1M count=256 >/dev/null 2>&1 ; \
	  done ; \
	done
	rm -f $$(find testtree.tmp -type f -name "date*txt" -o -name "dd*.dat" | grep COLD_STORAGE)
	mkdir -p testtree
	mv testtree.tmp testtree/$$(date +%Y-%m-%d)
	date > $@

clean:
	rm -f .testdirstamp *::*::*

clobber:
	$(MAKE) clean
	rm -rf ./glade/
