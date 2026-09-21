/*
 * ktouch: virtual touchscreen for the Kindle via /dev/input/uinput,
 * with tap, long press and swipe.
 *
 * Creates a single-touch absolute device (ABS_X/ABS_Y + BTN_TOUCH, no
 * BTN_TOOL_FINGER so the old evdev classifies it as a touchscreen).
 * Xorg adopts it through udev when ID_INPUT is set for its name.
 *
 * Commands on stdin (daemon mode), one per line:
 *   X Y [hold_ms]            tap (hold_ms > 0 = long press)
 *   S X1 Y1 X2 Y2 [ms]       swipe from (X1,Y1) to (X2,Y2) over ms
 *
 * Build: make (arm-linux-gnueabihf-gcc -static -O2 -o ktouch ktouch.c)
 */
#include <fcntl.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define W 1072
#define H 1448

static int fd = -1;
static int tracking = 100;

static void die(const char *m) { perror(m); exit(1); }

static void msleep(int ms) {
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

static void emit(int type, int code, int value) {
    struct input_event ev;
    memset(&ev, 0, sizeof ev);
    ev.type = type; ev.code = code; ev.value = value;
    if (write(fd, &ev, sizeof ev) != sizeof ev) die("write event");
}

static void abs_setup(int code, int min, int max) {
    struct uinput_abs_setup a;
    memset(&a, 0, sizeof a);
    a.code = code; a.absinfo.minimum = min; a.absinfo.maximum = max;
    if (ioctl(fd, UI_ABS_SETUP, &a) < 0) die("UI_ABS_SETUP");
}

static void create_device(void) {
    fd = open("/dev/input/uinput", O_WRONLY | O_NONBLOCK);
    if (fd < 0) fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (fd < 0) die("open uinput");
    if (ioctl(fd, UI_SET_EVBIT, EV_KEY) < 0) die("EV_KEY");
    if (ioctl(fd, UI_SET_EVBIT, EV_ABS) < 0) die("EV_ABS");
    if (ioctl(fd, UI_SET_EVBIT, EV_SYN) < 0) die("EV_SYN");
    if (ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH) < 0) die("BTN_TOUCH");
    if (ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT) < 0) die("INPUT_PROP_DIRECT");
    abs_setup(ABS_X, 0, W - 1);
    abs_setup(ABS_Y, 0, H - 1);
    struct uinput_setup us;
    memset(&us, 0, sizeof us);
    us.id.bustype = BUS_I2C;
    us.id.vendor = 0x0416; us.id.product = 0x1002; us.id.version = 2;
    snprintf(us.name, sizeof us.name, "ktouch");   /* matched by the udev rule in kindle-setup.sh */
    if (ioctl(fd, UI_DEV_SETUP, &us) < 0) die("UI_DEV_SETUP");
    if (ioctl(fd, UI_DEV_CREATE) < 0) die("UI_DEV_CREATE");
    msleep(300);
}

static void clampxy(int *x, int *y) {
    if (*x < 0) *x = 0;
    if (*x >= W) *x = W - 1;
    if (*y < 0) *y = 0;
    if (*y >= H) *y = H - 1;
}

static void tap(int x, int y, int hold_ms) {
    int id = tracking++;
    clampxy(&x, &y);
    emit(EV_KEY, BTN_TOUCH, 1);
    emit(EV_ABS, ABS_X, x);
    emit(EV_ABS, ABS_Y, y);
    emit(EV_SYN, SYN_REPORT, 0);
    msleep(hold_ms > 0 ? hold_ms : 45);
    emit(EV_KEY, BTN_TOUCH, 0);
    emit(EV_SYN, SYN_REPORT, 0);
    fprintf(stderr, "tap %d,%d hold=%dms id=%d\n", x, y, hold_ms > 0 ? hold_ms : 45, id);
}

static void swipe(int x1, int y1, int x2, int y2, int ms) {
    int id = tracking++;
    clampxy(&x1, &y1); clampxy(&x2, &y2);
    if (ms < 60) ms = 60;
    int steps = ms / 15;
    if (steps < 4) steps = 4;
    emit(EV_KEY, BTN_TOUCH, 1);
    emit(EV_ABS, ABS_X, x1);
    emit(EV_ABS, ABS_Y, y1);
    emit(EV_SYN, SYN_REPORT, 0);
    for (int i = 1; i <= steps; i++) {
        msleep(ms / steps);
        emit(EV_ABS, ABS_X, x1 + (x2 - x1) * i / steps);
        emit(EV_ABS, ABS_Y, y1 + (y2 - y1) * i / steps);
        emit(EV_SYN, SYN_REPORT, 0);
    }
    msleep(20);
    emit(EV_KEY, BTN_TOUCH, 0);
    emit(EV_SYN, SYN_REPORT, 0);
    fprintf(stderr, "swipe %d,%d -> %d,%d %dms id=%d\n", x1, y1, x2, y2, ms, id);
}

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "daemon") == 0) {
        create_device();
        fprintf(stderr, "ktouch daemon: device up (tap + swipe)\n");
        char line[160];
        while (fgets(line, sizeof line, stdin)) {
            int x, y, hold = 0, x2, y2, ms = 200;
            if (line[0] == 'S' || line[0] == 's') {
                if (sscanf(line + 1, "%d %d %d %d %d", &x, &y, &x2, &y2, &ms) >= 4) swipe(x, y, x2, y2, ms);
            } else if (sscanf(line, "%d %d %d", &x, &y, &hold) >= 2) {
                tap(x, y, hold);
            }
        }
        msleep(200);
        ioctl(fd, UI_DEV_DESTROY);
        return 0;
    }
    fprintf(stderr, "usage: ktouch daemon   (reads 'X Y [hold]' or 'S X1 Y1 X2 Y2 [ms]' from stdin)\n");
    return 2;
}
