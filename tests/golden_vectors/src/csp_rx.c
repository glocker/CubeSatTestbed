/*
 * A libcsp node that only listens: the receiving counterpart of csp_client.
 *
 * Usage: csp_rx <can:IFACE | kiss:DEVICE | zmq:HOST> <address> <version> <port|any> <seconds>
 *
 * Opens the named libcsp interface as node <address> speaking CSP <version> (1 or 2), binds a
 * socket to <port> (or to every port), and prints one line per packet it receives for
 * <seconds> seconds, then the total:
 *
 *   RX src=2 dst=1 dport=10 sport=10 len=4 first=42
 *   TOTAL 1
 *
 * The CAN interface is opened without promiscuous mode, so libcsp installs its own destination
 * filter exactly as it does on a real node: a frame not addressed to <address> never arrives.
 * That is the point of the helper. The interop tests use it to check that what the testbed puts
 * on a link is something a real libcsp node accepts. Mind that libcsp can only bind ports up to
 * CSP_PORT_MAX_BIND (16 in the default build).
 */
#include <csp/csp.h>
#include <csp/drivers/can_socketcan.h>
#include <csp/drivers/usart.h>
#include <csp/interfaces/csp_if_zmqhub.h>

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "csp_posix_helper.h"

#define KISS_BAUDRATE 115200U
/* Time for a ZeroMQ PUB/SUB subscription to reach the hub before packets are expected. */
#define ZMQ_SETTLE_US 1000000U
#define ACCEPT_TIMEOUT_MS 500U
#define READ_TIMEOUT_MS 50U
#define BACKLOG 10

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
    if (argc != 6) {
        fprintf(stderr,
                "Usage: %s <can:IFACE | kiss:DEVICE | zmq:HOST> <address> <version> <port|any> "
                "<seconds>\n",
                argv[0]);
        return 2;
    }
    const char *spec = argv[1];
    uint16_t address = (uint16_t)atoi(argv[2]);
    int version = atoi(argv[3]);
    int port = (strcmp(argv[4], "any") == 0) ? CSP_ANY : atoi(argv[4]);
    int seconds = atoi(argv[5]);
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
        return 1;
    }
    iface->is_default = 1;
    router_start();

    csp_socket_t socket = {0};
    csp_bind(&socket, port);
    csp_listen(&socket, BACKLOG);

    int received = 0;
    time_t end = time(NULL) + seconds;
    while (time(NULL) < end) {
        csp_conn_t *connection = csp_accept(&socket, ACCEPT_TIMEOUT_MS);
        if (connection == NULL) {
            continue;
        }
        csp_packet_t *packet;
        while ((packet = csp_read(connection, READ_TIMEOUT_MS)) != NULL) {
            printf("RX src=%d dst=%d dport=%d sport=%d len=%u first=%02x\n",
                   csp_conn_src(connection),
                   csp_conn_dst(connection),
                   csp_conn_dport(connection),
                   csp_conn_sport(connection),
                   (unsigned)packet->length,
                   packet->length ? packet->data[0] : 0U);
            fflush(stdout);
            csp_buffer_free(packet);
            received++;
        }
        csp_close(connection);
    }
    printf("TOTAL %d\n", received);
    return 0;
}
