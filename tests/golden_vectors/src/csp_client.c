#include <csp/csp.h>
#include <csp/csp_error.h>
#include <csp/csp_rtable.h>
#include <csp/drivers/can_socketcan.h>

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define DEFAULT_CAN_INTERFACE "vcan0"
#define DEFAULT_SOURCE_ADDRESS 1U
#define DEFAULT_DESTINATION_ADDRESS 2U
#define PING_PAYLOAD_BYTE 0x55U

/** Runtime options parsed from the vector helper command line. */
struct options {
    const char *can_interface;
    uint16_t source_address;
    uint16_t destination_address;
    bool send_ping;
};

/**
 * Print command-line usage for user running the helper inside the vector container.
 *
 * @param stream Output stream used for usage text.
 * @param program Program name from argv[0].
 */
static void print_usage(FILE *stream, const char *program) {
    fprintf(stream,
            "Usage: %s -p [-c can-interface] [-a source-address] [-d destination-address]\n"
            "\n"
            "Generate libcsp CSP v2 SocketCAN traffic for repository golden vectors.\n"
            "\n"
            "Options:\n"
            "  -p                      send one single-frame CSP ping request\n"
            "  -c <can-interface>      SocketCAN interface (default: %s)\n"
            "  -a <source-address>     CSP source/interface address (default: %u)\n"
            "  -d <destination-address> CSP destination address (default: %u)\n"
            "  -h                      show this help\n",
            program,
            DEFAULT_CAN_INTERFACE,
            DEFAULT_SOURCE_ADDRESS,
            DEFAULT_DESTINATION_ADDRESS);
}

/**
 * Parse an unsigned 16-bit command-line value with an explicit upper bound.
 *
 * strtoul() is used with base 0 so decimal and C-style prefixed values such as
 * 0x2 are both accepted. The upper bound lets callers enforce CSP v2 field
 * widths at the CLI boundary instead of passing invalid values into libcsp.
 *
 * @param value NUL-terminated CLI argument to parse.
 * @param max_value Largest accepted value for this specific field.
 * @param result Destination for the parsed value on success.
 * @return true if value contains exactly one in-range integer, otherwise false.
 */
static bool parse_u16(const char *value, uint16_t max_value, uint16_t *result) {
    char *end = NULL;
    errno = 0;
    unsigned long parsed = strtoul(value, &end, 0);
    if ((errno != 0) || (end == value) || (*end != '\0') || (parsed > max_value)) {
        return false;
    }

    *result = (uint16_t)parsed;
    return true;
}

/**
 * Decode supported helper options into the provided options structure.
 *
 * The caller initializes defaults before calling this function; parse_args()
 * only applies explicit overrides and validates that one vector action was
 * selected. This keeps defaults visible in main() while keeping option parsing
 * separate from libcsp setup and packet transmission.
 *
 * @param argc Argument count from main().
 * @param argv Argument vector from main().
 * @param options Mutable options structure initialized by the caller.
 * @return true when all arguments are valid and a vector action is selected.
 */
static bool parse_args(int argc, char **argv, struct options *options) {
    int opt;
    while ((opt = getopt(argc, argv, "c:a:d:ph")) != -1) {
        switch (opt) {
            case 'c':
                options->can_interface = optarg;
                break;
            case 'a':
                if (!parse_u16(optarg, 0x3FFFU, &options->source_address)) {
                    fprintf(stderr, "Invalid source address: %s\n", optarg);
                    return false;
                }
                break;
            case 'd':
                if (!parse_u16(optarg, 0x3FFFU, &options->destination_address)) {
                    fprintf(stderr, "Invalid destination address: %s\n", optarg);
                    return false;
                }
                break;
            case 'p':
                options->send_ping = true;
                break;
            case 'h':
                print_usage(stdout, argv[0]);
                exit(EXIT_SUCCESS);
            default:
                return false;
        }
    }

    if (!options->send_ping) {
        fprintf(stderr, "No vector action selected; pass -p to send the ping vector.\n");
        return false;
    }

    return true;
}

/**
 * Send the fixed CSP ping request used by the first committed golden vector.
 *
 * The packet is emitted through the official libcsp SocketCAN path, not by
 * manually constructing a CAN frame, so the captured bytes remain a libcsp-owned
 * source of truth for the future Python codec. csp_connect()/csp_send() are used
 * instead of csp_ping() because the vector must be a one-byte, no-reply,
 * no-CRC, single-frame packet.
 *
 * @param can_iface libcsp CAN interface used for transmission and TX accounting.
 * @param destination_address CSP destination node address for the ping request.
 * @return EXIT_SUCCESS after libcsp reports one transmitted packet, otherwise
 * EXIT_FAILURE.
 */
static int send_ping_request(csp_iface_t *can_iface, uint16_t destination_address) {
    uint32_t tx_before = can_iface->tx;
    uint32_t tx_error_before = can_iface->tx_error;

    csp_conn_t *connection = csp_connect(
        CSP_PRIO_NORM,
        destination_address,
        CSP_PING,
        0,
        CSP_O_NONE);
    if (connection == NULL) {
        fprintf(stderr, "Failed to create CSP ping connection.\n");
        return EXIT_FAILURE;
    }

    csp_packet_t *packet = csp_buffer_get(0);
    if (packet == NULL) {
        fprintf(stderr, "Failed to allocate CSP packet buffer.\n");
        csp_close(connection);
        return EXIT_FAILURE;
    }

    packet->data[0] = PING_PAYLOAD_BYTE;
    packet->length = 1;

    csp_send(connection, packet);
    csp_close(connection);

    /*
     * libcsp send APIs return void here, so use interface counters to fail fast
     * on silent TX errors.
     */
    if ((can_iface->tx == tx_before) || (can_iface->tx_error != tx_error_before)) {
        fprintf(stderr, "Failed to transmit CSP ping request on SocketCAN.\n");
        return EXIT_FAILURE;
    }

    return EXIT_SUCCESS;
}

/**
 * Configure libcsp for CSP v2 over SocketCAN and emit the selected golden-vector packet.
 *
 * @param argc Argument count from the process entry point.
 * @param argv Argument vector from the process entry point.
 * @return EXIT_SUCCESS after the selected vector is sent, otherwise EXIT_FAILURE.
 */
int main(int argc, char **argv) {
    struct options options = {
        .can_interface = DEFAULT_CAN_INTERFACE,
        .source_address = DEFAULT_SOURCE_ADDRESS,
        .destination_address = DEFAULT_DESTINATION_ADDRESS,
        .send_ping = false,
    };

    if (!parse_args(argc, argv, &options)) {
        print_usage(stderr, argv[0]);
        return EXIT_FAILURE;
    }

    /*
     * libcsp reads csp_conf.version during initialization; keep the helper pinned
     * to CSP v2.
     */
    csp_conf.version = 2;
    csp_init();

    /*
     * Bitrate 0 avoids reconfiguring vcan0, which does not need a physical CAN
     * bitrate.
     * Promiscuous mode keeps the helper usable for capture-only vector generation.
     */
    csp_iface_t *can_iface = NULL;
    int error = csp_can_socketcan_open_and_add_interface(
        options.can_interface,
        CSP_IF_CAN_DEFAULT_NAME,
        options.source_address,
        0,
        true,
        &can_iface);
    if (error != CSP_ERR_NONE) {
        fprintf(stderr,
                "Failed to open SocketCAN interface %s: libcsp error %d\n",
                options.can_interface,
                error);
        return EXIT_FAILURE;
    }

    can_iface->is_default = 1;

#if (CSP_USE_RTABLE)
    /* Route every destination through the generated SocketCAN interface. */
    error = csp_rtable_set(0, 0, can_iface, CSP_NO_VIA_ADDRESS);
    if (error != CSP_ERR_NONE) {
        fprintf(stderr, "Failed to configure libcsp default route: error %d\n", error);
        return EXIT_FAILURE;
    }
#endif

    if (options.send_ping) {
        return send_ping_request(can_iface, options.destination_address);
    }

    return EXIT_SUCCESS;
}
