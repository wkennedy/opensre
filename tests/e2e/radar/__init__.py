"""End-to-end tests for the Radar Kubernetes MCP bridge integration.

These exercise the bridge against a *live* Radar instance (and, for the full-RCA
smoke, a live LLM). They are excluded from the default offline suite via
``norecursedirs`` in ``pytest.ini`` and gated behind ``RADAR_MCP_URL`` so they
skip cleanly when the prerequisites are absent. See ``README.md``.
"""
