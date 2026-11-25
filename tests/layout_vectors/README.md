# Telemetry layout vectors

Independent byte sequences for the telemetry codec (backlog item 17), in the spirit of the
golden vectors: a capture file and a sibling `*.meta.toml` saying where it came from and what
it must decode to. The testbed must decode the bytes to the expected values and encode those
values back to the same bytes.

| Vector | Bytes from | Expected values from |
| --- | --- | --- |
| `qube_telemetry_payload` | `scripts/generate-qube-vector.py` (`struct` and two hand-picked flag bytes) | the Kaitai Struct parser `satnogs-decoders` generates from `qube.ksy` |

The CSP headers inside the golden vectors under `../golden_vectors/` are a second source: they
were produced by libcsp `v2.1`, and the tests in `tests/test_layout_vectors.py` decode them
as bit fields (2-bit priority, 5- or 14-bit addresses, 6-bit ports) that cross byte boundaries.

`satnogs-decoders` is not a project dependency. To regenerate the QUBE vector:

```sh
python3 -m venv /tmp/oracle && /tmp/oracle/bin/pip install satnogs-decoders==1.132.1
/tmp/oracle/bin/python -I tests/layout_vectors/scripts/generate-qube-vector.py tests/layout_vectors
```
