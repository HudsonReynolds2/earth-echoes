"""Configuration model (epic E2; spec 5): catalog, overrides, merge engine.

Follows the app.inventory pattern: pure cores with no database imports next
to thin DB services, return-based results, stage-never-commit. Signatures
here are published contracts (docs/INTERFACES.md, "Owned by E2").
"""
