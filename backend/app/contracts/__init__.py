"""Wire contracts shared with things outside this codebase.

Modules here are PUBLISHED INTERFACES: the simulation harness (SIM epic)
imports them directly, and device firmware is written against them. Treat
every symbol as load-bearing from the moment it merges — additive change
only, and never a silent rename.
"""
