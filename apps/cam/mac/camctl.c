// camctl: the C922's focus, over USB Video Class (UVC) controls.
//
//   camctl status            autofocus on or off, focus position and range
//   camctl set auto          autofocus on, and remember that
//   camctl set <0-250>       autofocus off at that focus, and remember it
//   camctl apply             re-apply what was remembered (cam-capture runs
//                            this each time it starts the camera)
//
// macOS has no API for a webcam's focus, but the camera takes the standard
// UVC requests for it - sent here as control transfers through IOKit, which
// needs no root and works while the camera is streaming. The C922 keeps a
// setting until it loses power; the remembered one (~/.config/homelab-cam/
// focus) covers a replug or a reboot.
//
// Focus on the C922: 0 is far (infinity), 250 the closest it can focus, in
// steps of 5. Built by install.py with the Command Line Tools' clang.

#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/usb/IOUSBLib.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define VENDOR 0x046D   // Logitech
#define PRODUCT 0x085C  // C922 Pro Stream

// UVC: the camera terminal is unit 1 on the video control interface 0.
#define UNIT 1
#define INTERFACE 0
#define SET_CUR 0x01
#define GET_CUR 0x81
#define GET_MIN 0x82
#define GET_MAX 0x83
#define GET_RES 0x84
#define FOCUS_ABSOLUTE 0x06  // 2 bytes
#define FOCUS_AUTO 0x08      // 1 byte

static IOUSBDeviceInterface **camera(void) {
    CFMutableDictionaryRef match = IOServiceMatching("IOUSBHostDevice");
    int vendor = VENDOR, product = PRODUCT;
    CFNumberRef v = CFNumberCreate(NULL, kCFNumberIntType, &vendor);
    CFNumberRef p = CFNumberCreate(NULL, kCFNumberIntType, &product);
    CFDictionarySetValue(match, CFSTR("idVendor"), v);
    CFDictionarySetValue(match, CFSTR("idProduct"), p);
    CFRelease(v);
    CFRelease(p);
    io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, match);
    if (!service)
        return NULL;
    IOCFPlugInInterface **plugin = NULL;
    SInt32 score;
    kern_return_t kr = IOCreatePlugInInterfaceForService(
        service, kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID, &plugin, &score);
    IOObjectRelease(service);
    if (kr != KERN_SUCCESS || !plugin)
        return NULL;
    IOUSBDeviceInterface **dev = NULL;
    (*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID), (LPVOID *)&dev);
    (*plugin)->Release(plugin);
    return dev;
}

// One UVC class request to the camera terminal. Returns bytes moved, or -1.
static int control(IOUSBDeviceInterface **dev, UInt8 request, UInt8 selector, void *data, UInt16 length) {
    int in = request & 0x80;
    IOUSBDevRequest r = {
        .bmRequestType = USBmakebmRequestType(in ? kUSBIn : kUSBOut, kUSBClass, kUSBInterface),
        .bRequest = request,
        .wValue = (UInt16)(selector << 8),
        .wIndex = (UInt16)((UNIT << 8) | INTERFACE),
        .wLength = length,
        .pData = data,
    };
    return (*dev)->DeviceRequest(dev, &r) == kIOReturnSuccess ? (int)r.wLenDone : -1;
}

static int read_focus(IOUSBDeviceInterface **dev, UInt8 request, UInt16 *value) {
    *value = 0;
    return control(dev, request, FOCUS_ABSOLUTE, value, 2) == 2 ? 0 : -1;
}

static int set_auto(IOUSBDeviceInterface **dev) {
    UInt8 on = 1;
    return control(dev, SET_CUR, FOCUS_AUTO, &on, 1) == 1 ? 0 : -1;
}

static int set_manual(IOUSBDeviceInterface **dev, UInt16 focus) {
    UInt8 off = 0;
    if (control(dev, SET_CUR, FOCUS_AUTO, &off, 1) != 1)
        return -1;
    return control(dev, SET_CUR, FOCUS_ABSOLUTE, &focus, 2) == 2 ? 0 : -1;
}

static const char *saved_path(void) {
    static char path[1024];
    snprintf(path, sizeof path, "%s/.config/homelab-cam/focus", getenv("HOME") ? getenv("HOME") : ".");
    return path;
}

// "auto" or a focus value, checked against the camera's own range.
static int parse(IOUSBDeviceInterface **dev, const char *text, int *is_auto, UInt16 *focus) {
    if (strcmp(text, "auto") == 0) {
        *is_auto = 1;
        return 0;
    }
    char *end;
    errno = 0;
    long value = strtol(text, &end, 10);
    UInt16 min = 0, max = 250, step = 5;
    read_focus(dev, GET_MIN, &min);
    read_focus(dev, GET_MAX, &max);
    read_focus(dev, GET_RES, &step);
    if (errno || *end || value < min || value > max) {
        fprintf(stderr, "camctl: focus is \"auto\" or %u-%u\n", min, max);
        return -1;
    }
    if (step > 1)
        value = (value / step) * step;  // the camera only takes its own steps
    *is_auto = 0;
    *focus = (UInt16)value;
    return 0;
}

static int apply(IOUSBDeviceInterface **dev, int is_auto, UInt16 focus) {
    if ((is_auto ? set_auto(dev) : set_manual(dev, focus)) != 0) {
        fprintf(stderr, "camctl: the camera refused the setting\n");
        return 1;
    }
    return 0;
}

static int status(IOUSBDeviceInterface **dev) {
    UInt8 is_auto = 0;
    UInt16 cur, min, max, step;
    if (control(dev, GET_CUR, FOCUS_AUTO, &is_auto, 1) != 1 || read_focus(dev, GET_CUR, &cur) ||
        read_focus(dev, GET_MIN, &min) || read_focus(dev, GET_MAX, &max) || read_focus(dev, GET_RES, &step)) {
        fprintf(stderr, "camctl: the camera didn't answer\n");
        return 1;
    }
    char saved[32] = "none";
    FILE *f = fopen(saved_path(), "r");
    if (f) {
        if (fscanf(f, "%31s", saved) != 1)
            strcpy(saved, "none");
        fclose(f);
    }
    printf("autofocus: %s\n", is_auto ? "on" : "off");
    printf("focus:     %u (range %u-%u, step %u; 0 is far)\n", cur, min, max, step);
    printf("saved:     %s\n", saved);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2 || (strcmp(argv[1], "set") == 0 && argc < 3)) {
        fprintf(stderr, "usage: camctl status | set auto | set <focus> | apply\n");
        return 2;
    }
    IOUSBDeviceInterface **dev = camera();
    if (!dev) {
        fprintf(stderr, "camctl: no C922 found\n");
        return 1;
    }
    int is_auto;
    UInt16 focus = 0;

    if (strcmp(argv[1], "status") == 0)
        return status(dev);

    if (strcmp(argv[1], "set") == 0) {
        if (parse(dev, argv[2], &is_auto, &focus))
            return 2;
        if (apply(dev, is_auto, focus))
            return 1;
        FILE *f = fopen(saved_path(), "w");
        if (f) {
            if (is_auto)
                fprintf(f, "auto\n");
            else
                fprintf(f, "%u\n", focus);
            fclose(f);
        }
        return status(dev);
    }

    if (strcmp(argv[1], "apply") == 0) {
        char saved[32];
        FILE *f = fopen(saved_path(), "r");
        if (!f)
            return 0;  // nothing remembered: leave the camera as it is
        int ok = fscanf(f, "%31s", saved) == 1;
        fclose(f);
        if (!ok || parse(dev, saved, &is_auto, &focus))
            return 2;
        return apply(dev, is_auto, focus);
    }

    fprintf(stderr, "usage: camctl status | set auto | set <focus> | apply\n");
    return 2;
}
