"""Risk core: the Unified Risk Rulebook (risk/limits.yaml) made executable.

Nothing in this package talks to a broker. Everything an order needs to pass
lives in ``engine.RiskEngine.approve`` (CLAUDE.md prime directive 2).
"""
