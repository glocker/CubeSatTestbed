"""Scenario runner over deterministic virtual time.

A scenario is an ordered YAML script containing actions such as fault injection,
virtual waits, command sends, and assertions. The runner schedules these actions
on the DES engine and collects PASS/FAIL results.
"""
