SUBDIRS := c cpp objc

.PHONY: all check clean $(SUBDIRS)

all check clean:
	@for dir in $(SUBDIRS); do \
		echo "==> $$dir: $@"; \
		$(MAKE) -C $$dir $@ || exit $$?; \
	done

