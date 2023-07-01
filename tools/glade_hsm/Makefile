all: .testdirstamp

.testdirstamp: Makefile
	mkdir -p glade/campaign/testtree/{sample1,sample2,foo,bar,COLD_STORAGE/baz,COLD_STORAGE/bar}
	date > $@

clean:
	rm -f .testdirstamp

clobber:
	$(MAKE) clean
	rm -rf ./glade/
