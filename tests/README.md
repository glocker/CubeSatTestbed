# Planned test strategy

- CSP v2 header pack/unpack: round-trip tests, plus validation against
  frames captured from a real libcsp build (not just internal consistency)
- Per-module model tests (EPS discharge curve, payload power draw, OBC Peer
  rule engine)
- Fault injection: verify a triggered fault actually changes the module's
  telemetry output as expected
- Scenario engine: a few end-to-end scenarios run against simulated-only
  setups, checked against expected PASS/FAIL results
