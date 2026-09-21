/*
 * fbstream: low-latency framebuffer stream for the Kindle (11th gen, 8bpp).
 *
 * Reads /dev/fb0, downsamples 2x2 -> 536x724, packs two 4-bit pixels per
 * byte (16 gray levels, which is all the e-ink shows anyway) and writes
 * frames to stdout only when the content changed. Meant to run over a
 * persistent ssh connection: ssh kindle /mnt/us/fbstream | consumer.
 *
 * Wire format (little endian):
 *   'F' + u32 length + payload      new frame (length = 536*724/2 bytes)
 *   'S'                             nothing changed (keepalive)
 *
 * Build: arm-linux-gnueabihf-gcc -static -O2 -o fbstream fbstream.c
 */
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define W 1072
#define H 1448
#define STRIDE 1072
#define OW (W / 2)
#define OH (H / 2)
#define OUT_BYTES (OW * OH / 2)

static void msleep(int ms) {
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

static int write_all(const void *buf, size_t n) {
    const uint8_t *p = buf;
    while (n) {
        ssize_t w = write(1, p, n);
        if (w <= 0) return -1;
        p += w; n -= w;
    }
    return 0;
}

int main(int argc, char **argv) {
    int interval_ms = argc > 1 ? atoi(argv[1]) : 70;
    int fd = open("/dev/fb0", O_RDONLY);
    if (fd < 0) { perror("open /dev/fb0"); return 1; }

    static uint8_t fb[STRIDE * H];
    static uint8_t out[OUT_BYTES];
    uint64_t last_hash = 0;
    int keep = 0;

    for (;;) {
        if (lseek(fd, 0, SEEK_SET) < 0 || read(fd, fb, sizeof fb) != (ssize_t)sizeof fb) {
            perror("read fb0"); return 1;
        }
        /* 2x2 box filter + 4-bit pack, and a cheap rolling hash */
        uint64_t h = 1469598103934665603ULL;
        uint8_t *o = out;
        for (int y = 0; y < OH; y++) {
            const uint8_t *r0 = fb + (2 * y) * STRIDE;
            const uint8_t *r1 = r0 + STRIDE;
            for (int x = 0; x < OW; x += 2) {
                int a = (r0[2*x] + r0[2*x+1] + r1[2*x] + r1[2*x+1]) >> 6;          /* /4 then >>4 */
                int b = (r0[2*x+2] + r0[2*x+3] + r1[2*x+2] + r1[2*x+3]) >> 6;
                uint8_t v = (uint8_t)((a << 4) | b);
                *o++ = v;
                h = (h ^ v) * 1099511628211ULL;
            }
        }
        if (h != last_hash) {
            uint8_t hdr[5] = { 'F', OUT_BYTES & 255, (OUT_BYTES >> 8) & 255, (OUT_BYTES >> 16) & 255, (OUT_BYTES >> 24) & 255 };
            if (write_all(hdr, 5) || write_all(out, OUT_BYTES)) return 0;
            last_hash = h; keep = 0;
        } else if (++keep >= 4) {           /* keepalive a few times per second */
            if (write_all("S", 1)) return 0;
            keep = 0;
        }
        msleep(interval_ms);
    }
}
