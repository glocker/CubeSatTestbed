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
#define DEFAULT_PRIORITY CSP_PRIO_NORM
#define DEFAULT_PAYLOAD_LENGTH 1U
/*
 * Largest payload the helper will send. libcsp's default packet buffer holds
 * 256 bytes (CSP_BUFFER_SIZE); staying well below it leaves room for the CRC32
 * / HMAC trailer libcsp appends and keeps every vector a packet libcsp itself
 * would also accept on receive. Payloads over 4 bytes (or any payload carrying a
 * CRC32/HMAC trailer) leave the single-frame profile and are sent by libcsp as
 * several CFP2 frames.
 */
#define MAX_PAYLOAD_LENGTH 200U
#define DEFAULT_PACKET_COUNT 1U
#define MAX_PACKET_COUNT 8U
#define PAYLOAD_BASE_BYTE 0x55U

/** Runtime options parsed from the vector helper command line. */
struct options {
    const char *can_interface;
    uint16_t source_address;
    uint16_t destination_address;
    uint8_t priority;
    uint32_t csp_opts;
    uint16_t payload_length;
    uint16_t packet_count;
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
            "          [-r priority] [-l payload-length] [-o flag[,flag...]] [-n packets]\n"
            "\n"
            "Generate libcsp CSP v2 SocketCAN traffic for repository golden vectors.\n"
            "\n"
            "Options:\n"
            "  -p                      send CSP ping-port request(s); each becomes one CAN frame\n"
            "                          when it fits, or several CFP2 frames when it does not\n"
            "  -c <can-interface>      SocketCAN interface (default: %s)\n"
            "  -a <source-address>     CSP source/interface address (default: %u)\n"
            "  -d <destination-address> CSP destination address (default: %u)\n"
            "  -r <priority>           CSP priority 0-3: 0=critical 1=high 2=norm 3=low\n"
            "                          (default: %u)\n"
            "  -l <payload-length>     payload bytes, 0-%u (default: %u); filled with an\n"
            "                          incrementing pattern starting at 0x%02x\n"
            "  -o <flag[,flag...]>     comma-separated CSP options: crc32, rdp, hmac\n"
            "                          (default: none)\n"
            "  -n <packets>            send this many identical packets back to back, 1-%u\n"
            "                          (default: %u); consecutive packets carry an\n"
            "                          incrementing CFP2 packet counter\n"
            "  -h                      show this help\n",
            program,
            DEFAULT_CAN_INTERFACE,
            DEFAULT_SOURCE_ADDRESS,
            DEFAULT_DESTINATION_ADDRESS,
            (unsigned)DEFAULT_PRIORITY,
            MAX_PAYLOAD_LENGTH,
            DEFAULT_PAYLOAD_LENGTH,
            PAYLOAD_BASE_BYTE,
            MAX_PACKET_COUNT,
            DEFAULT_PACKET_COUNT);
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
 * Parse a comma-separated list of named CSP connection options into a libcsp
 * options bitmask.
 *
 * @param value Comma-separated flag names: crc32, rdp, hmac.
 * @param result Destination for the combined CSP_O_* bitmask on success.
 * @return true if every name in ``value`` is recognized, otherwise false.
 */
static bool parse_csp_opts(const char *value, uint32_t *result) {
    uint32_t opts = CSP_O_NONE;
    char buffer[128];
    if (strlen(value) >= sizeof(buffer)) {
        return false;
    }
    strcpy(buffer, value);

    char *saveptr = NULL;
    char *token = strtok_r(buffer, ",", &saveptr);
    while (token != NULL) {
        if (strcmp(token, "crc32") == 0) {
            opts |= CSP_O_CRC32;
        } else if (strcmp(token, "rdp") == 0) {
            opts |= CSP_O_RDP;
        } else if (strcmp(token, "hmac") == 0) {
            opts |= CSP_O_HMAC;
        } else {
            fprintf(stderr, "Unknown CSP option flag: %s\n", token);
            return false;
        }
        token = strtok_r(NULL, ",", &saveptr);
    }

    *result = opts;
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
    uint16_t parsed_u16;
    while ((opt = getopt(argc, argv, "c:a:d:r:l:o:n:ph")) != -1) {
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
            case 'r':
                if (!parse_u16(optarg, 3U, &parsed_u16)) {
                    fprintf(stderr, "Invalid priority (0-3): %s\n", optarg);
                    return false;
                }
                options->priority = (uint8_t)parsed_u16;
                break;
            case 'l':
                if (!parse_u16(optarg, MAX_PAYLOAD_LENGTH, &parsed_u16)) {
                    fprintf(stderr, "Invalid payload length (0-%u): %s\n", MAX_PAYLOAD_LENGTH, optarg);
                    return false;
                }
                options->payload_length = parsed_u16;
                break;
            case 'o':
                if (!parse_csp_opts(optarg, &options->csp_opts)) {
                    return false;
                }
                break;
            case 'n':
                if (!parse_u16(optarg, MAX_PACKET_COUNT, &parsed_u16) || (parsed_u16 == 0U)) {
                    fprintf(stderr, "Invalid packet count (1-%u): %s\n", MAX_PACKET_COUNT, optarg);
                    return false;
                }
                options->packet_count = parsed_u16;
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
        fprintf(stderr, "No vector action selected; pass -p to send the ping-port vector.\n");
        return false;
    }

    return true;
}

/**
 * Send the configured CSP request(s) used by the committed golden vectors.
 *
 * The packets are emitted through the official libcsp SocketCAN path, not by
 * manually constructing CAN frames, so the captured bytes remain a libcsp-owned
 * source of truth for the Python codec. csp_connect()/csp_send() are used
 * instead of csp_ping() so every vector stays a no-reply packet regardless of
 * requested priority/options/payload length. Whether a packet becomes one CAN
 * frame or several CFP2 fragments is decided by libcsp from its length (payload
 * plus any CRC32/HMAC trailer), never by this helper.
 *
 * @param can_iface libcsp CAN interface used for transmission and TX accounting.
 * @param options Parsed command-line options selecting destination, priority,
 * CSP connection options, payload length/content and packet count.
 * @return EXIT_SUCCESS after libcsp reports every packet transmitted, otherwise
 * EXIT_FAILURE.
 */
static int send_vector_packet(csp_iface_t *can_iface, const struct options *options) {
    uint32_t tx_before = can_iface->tx;
    uint32_t tx_error_before = can_iface->tx_error;

    for (uint16_t sent = 0; sent < options->packet_count; sent++) {
        csp_conn_t *connection = csp_connect(
            options->priority,
            options->destination_address,
            CSP_PING,
            0,
            options->csp_opts);
        if (connection == NULL) {
            fprintf(stderr, "Failed to create CSP connection.\n");
            return EXIT_FAILURE;
        }

        csp_packet_t *packet = csp_buffer_get(0);
        if (packet == NULL) {
            fprintf(stderr, "Failed to allocate CSP packet buffer.\n");
            csp_close(connection);
            return EXIT_FAILURE;
        }

        for (uint16_t i = 0; i < options->payload_length; i++) {
            packet->data[i] = (uint8_t)(PAYLOAD_BASE_BYTE + i);
        }
        packet->length = options->payload_length;

        csp_send(connection, packet);
        csp_close(connection);
    }

    /*
     * libcsp send APIs return void here, so use interface counters to fail fast
     * on silent TX errors. tx counts packets, not CAN frames.
     */
    if (((can_iface->tx - tx_before) != options->packet_count) ||
        (can_iface->tx_error != tx_error_before)) {
        fprintf(stderr, "Failed to transmit CSP request on SocketCAN.\n");
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
        .priority = DEFAULT_PRIORITY,
        .csp_opts = CSP_O_NONE,
        .payload_length = DEFAULT_PAYLOAD_LENGTH,
        .packet_count = DEFAULT_PACKET_COUNT,
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
        return send_vector_packet(can_iface, &options);
    }

    return EXIT_SUCCESS;
}
