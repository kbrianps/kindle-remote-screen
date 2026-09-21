# Cross-compiles the two small programs that run on the Kindle and copies
# them to its user storage. Static binaries, so nothing else is needed there.
#
#   make                 build build/ktouch and build/fbstream
#   make install         copy them to /mnt/us on the Kindle (HOST=kindle)
#   make clean

CROSS   ?= arm-linux-gnueabihf-
CC      := $(CROSS)gcc
CFLAGS  ?= -static -O2 -Wall
HOST    ?= kindle
BINS    := build/ktouch build/fbstream

all: $(BINS)

build/%: kindle/%.c
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $<

install: $(BINS)
	scp $(BINS) $(HOST):/mnt/us/
	ssh $(HOST) 'chmod +x /mnt/us/ktouch /mnt/us/fbstream'

clean:
	rm -rf build

.PHONY: all install clean
