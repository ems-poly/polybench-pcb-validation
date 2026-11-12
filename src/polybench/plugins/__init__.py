"""Polybench plug-ins: actions (instruments, programmer, CAN, camera) and ECAD bridges.

Registered through entry points `polybench.actions` and `polybench.ecad` in pyproject.toml. Each action class has a
zero-argument constructor, a `name`, `capabilities`, `run(ctx)`, `safe_state()` and `selftest()`.
"""
