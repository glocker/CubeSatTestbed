/*
 * Records what crosses a libcsp ZeroMQ hub, for the golden vectors.
 *
 * Usage: zmq_capture <hub-publish-endpoint> <messages> <timeout-seconds> <output-file>
 *
 * Connects a SUB socket (subscribed to everything, as libcsp's own zmqhub interface
 * is) to the hub's publish endpoint, e.g. tcp://127.0.0.1:7000, and writes each
 * message it receives as one line of space-separated upper-case hex. Exits 0 after
 * <messages> messages and 1 if the timeout expires first. It is the ZeroMQ
 * counterpart of candump for the CAN vectors: it only listens.
 */
#include <zmq.h>

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc != 5) {
        fprintf(stderr, "Usage: %s <endpoint> <messages> <timeout-seconds> <output-file>\n", argv[0]);
        return 2;
    }
    const char *endpoint = argv[1];
    long wanted = strtol(argv[2], NULL, 10);
    long timeout_seconds = strtol(argv[3], NULL, 10);
    const char *output_path = argv[4];
    if (wanted < 1 || timeout_seconds < 1) {
        fprintf(stderr, "messages and timeout-seconds must be positive\n");
        return 2;
    }

    void *context = zmq_ctx_new();
    void *subscriber = zmq_socket(context, ZMQ_SUB);
    int timeout_ms = (int)(timeout_seconds * 1000);
    if (zmq_setsockopt(subscriber, ZMQ_SUBSCRIBE, "", 0) != 0 ||
        zmq_setsockopt(subscriber, ZMQ_RCVTIMEO, &timeout_ms, sizeof(timeout_ms)) != 0 ||
        zmq_connect(subscriber, endpoint) != 0) {
        fprintf(stderr, "Failed to set up the subscriber on %s: %s\n", endpoint, zmq_strerror(zmq_errno()));
        return 1;
    }

    FILE *output = fopen(output_path, "w");
    if (output == NULL) {
        perror("fopen");
        return 1;
    }

    static uint8_t buffer[4096];
    long received = 0;
    while (received < wanted) {
        int size = zmq_recv(subscriber, buffer, sizeof(buffer), 0);
        if (size < 0) {
            fprintf(stderr, "Captured %ld of %ld message(s): %s\n", received, wanted, zmq_strerror(zmq_errno()));
            fclose(output);
            return 1;
        }
        if ((size_t)size > sizeof(buffer)) {
            fprintf(stderr, "Message of %d bytes does not fit the capture buffer\n", size);
            fclose(output);
            return 1;
        }
        for (int i = 0; i < size; i++) {
            fprintf(output, i == 0 ? "%02X" : " %02X", buffer[i]);
        }
        fputc('\n', output);
        received++;
    }
    fclose(output);
    zmq_close(subscriber);
    zmq_ctx_destroy(context);
    return 0;
}
