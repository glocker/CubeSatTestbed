"""Run recording and the self-contained HTML report built from it."""

from cubesat_testbed.report.html_report import render_report, write_report
from cubesat_testbed.report.recorder import (
    RecordedAssertion,
    RecordedDrop,
    RecordedFrame,
    RecordedStep,
    RunRecorder,
    describe_step,
    format_virtual_time,
)

__all__ = [
    "RecordedAssertion",
    "RecordedDrop",
    "RecordedFrame",
    "RecordedStep",
    "RunRecorder",
    "describe_step",
    "format_virtual_time",
    "render_report",
    "write_report",
]
