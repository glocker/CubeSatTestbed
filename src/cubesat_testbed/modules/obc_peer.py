"""Reference OBC Peer module.

The OBC Peer is a stateless rule engine. It listens to decoded telemetry and
executes rules of the form ``IF <condition> THEN <action>``.

v1 supports threshold conditions, ``for`` duration, ``cooldown``, named command
emission through the configured codec/bus, and explicit calls into the passive
Fault Injection Engine. It does not wait for ACKs and does not implement
stateful on-enter/on-exit rules.
"""
