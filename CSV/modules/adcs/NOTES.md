# ADCS module -- not planned for v1

Attitude dynamics (quaternions, moment of inertia, sensor models) is a much
bigger scope than the other modules and risks turning this project into a
research effort instead of a usable tool.

The module interface (see `modules/base.py`) is designed so ADCS can be
added later as a module without changing the core engine. This file is a
placeholder for that possibility, not a commitment to a timeline.
