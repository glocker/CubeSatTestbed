/*
 * A libcsp node that pings: the request side of libcsp's ping service.
 *
 * Usage: csp_ping <can:IFACE | kiss:DEVICE | zmq:HOST> <address> <version> <target> <size> <timeout_ms> <count>
 *
 * Opens the named libcsp interface as node <address> speaking CSP <version> (1 or 2) and calls
 * libcsp's own csp_ping() <count> times against node <target> with a <size>-byte payload,
 * printing one line per attempt:
 *
 *   PING target=2 size=4 time=0ms
 *   PING target=2 size=4 timeout
 *
 * csp_ping() checks that the reply echoes the bytes it sent, so a line with a time means a
 * correct echo came back. The exit status is the number of failed attempts. The interop tests
 * use it to check that a simulated node answers ping the way a libcsp node does.
 */
#include <csp/csp.h>
#include <csp/drivers/can_socketcan.h>
#include <csp/drivers/usart.h>
#include <csp/interfaces/csp_if_zmqhub.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "csp_posix_helper.h"

#define KISS_BAUDRATE 115200U
/* Time for a ZeroMQ PUB/SUB subscription to reach the hub before the first ping. */
#define ZMQ_SETTLE_US 1000000U
#define PING_INTERVAL_US 200000U

static int open_interface(const char *spec, uint16_t address, csp_iface_t **iface) {
    if (strncmp(spec, "can:", 4) == 0) {
        return csp_can_socketcan_open_and_add_interface(
            spec + 4, CSP_IF_CAN_DEFAULT_NAME, address, 0, false, iface);
    }
    if (strncmp(spec, "kiss:", 5) == 0) {
        csp_usart_conf_t conf = {
            .device = spec + 5,
            .baudrate = KISS_BAUDRATE,
            .databits = 8,
            .stopbits = 1,
            .paritysetting = 0,
        };
        return csp_usart_open_and_add_kiss_interface(&conf, CSP_IF_KISS_DEFAULT_NAME, address, iface);
    }
    if (strncmp(spec, "zmq:", 4) == 0) {
        int error = csp_zmqhub_init(address, spec + 4, 0, iface);
        usleep(ZMQ_SETTLE_US);
        return error;
    }
    return CSP_ERR_INVAL;
}

int main(int argc, char **argv) {
    if (argc != 8) {
        fprintf(stderr,
                "Usage: %s <can:IFACE | kiss:DEVICE | zmq:HOST> <address> <version> <target> "
                "<size> <timeout_ms> <count>\n",
                argv[0]);
        return 2;
    }
    const char *spec = argv[1];
    uint16_t address = (uint16_t)atoi(argv[2]);
    int version = atoi(argv[3]);
    uint16_t target = (uint16_t)atoi(argv[4]);
    unsigned int size = (unsigned int)atoi(argv[5]);
    uint32_t timeout_ms = (uint32_t)atoi(argv[6]);
    int count = atoi(argv[7]);
    if ((version != 1) && (version != 2)) {
        fprintf(stderr, "CSP version must be 1 or 2, got %s\n", argv[3]);
        return 2;
    }

    csp_conf.version = (uint8_t)version;
    csp_init();

    csp_iface_t *iface = NULL;
    int error = open_interface(spec, address, &iface);
    if ((error != CSP_ERR_NONE) || (iface == NULL)) {
        fprintf(stderr, "Failed to open %s: libcsp error %d\n", spec, error);
        return 2;
    }
    iface->is_default = 1;
    router_start();

    int failures = 0;
    for (int attempt = 0; attempt < count; attempt++) {
        int time_ms = csp_ping(target, timeout_ms, size, CSP_O_NONE);
        if (time_ms < 0) {
            printf("PING target=%u size=%u timeout\n", (unsigned)target, size);
            failures++;
        } else {
            printf("PING target=%u size=%u time=%dms\n", (unsigned)target, size, time_ms);
        }
        fflush(stdout);
        usleep(PING_INTERVAL_US);
    }
    return failures;
}
