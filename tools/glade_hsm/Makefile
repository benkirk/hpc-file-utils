all: .testdirstamp

.testdirstamp: Makefile
	mkdir -p glade/campaign/testtree/{sample1,sample2,foo,bar,COLD_STORAGE/baz,COLD_STORAGE/bar}
	for dir in $$(find glade/campaign/testtree -type d); do \
	  for cnt in $$(seq 0 9); do \
	    date > $${dir}/date-$${cnt}.txt ; \
	  done ; \
	done
	rm -f $$(find glade/ -type f -name "date*txt" | grep COLD)
	date > $@

clean:
	rm -f .testdirstamp *::*::*

clobber:
	$(MAKE) clean
	rm -rf ./glade/
