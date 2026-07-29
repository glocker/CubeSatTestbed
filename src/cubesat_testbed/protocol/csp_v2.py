"""libcsp-compatible CSP v2 codec for product v1.

v1 target profile:

- classic CAN 2.0;
- extended 29-bit CAN identifiers;
- single-frame packets only;
- no fragmentation/reassembly;
- golden-vector validation against captures produced by the project-owned C helper built from official libcsp v2.1 commit 48f7fb0.

The public config exposes logical CSP fields: priority, source, destination,
destination port, source port and flags. Exact wire compatibility is defined by
committed fixtures in ``tests/golden_vectors/``.
"""
