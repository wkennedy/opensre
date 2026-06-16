# Radar bridge — end-to-end tests

Exercises OpenSRE's `radar` integration against a **live Radar instance** (the
Kubernetes visibility tool, https://github.com/skyhook-io/radar), proving the
Direction A wire end-to-end beyond the mocked unit tests in
`tests/integrations/test_radar.py` and `tests/tools/test_radar_mcp_tool.py`.

## Prerequisites

- A reachable Radar `/mcp` endpoint. Locally, run Radar against a cluster and
  point OpenSRE at it:
  ```bash
  # Radar (built from source) against the current kubeconfig, MCP on :9280
  docker run -d --name radar --network host \
    -v ~/.kube/config:/kube/config:ro radar:local \
    --no-browser --kubeconfig /kube/config --port 9280
  export RADAR_MCP_URL=http://localhost:9280/mcp     # --auth-mode none
  ```
- If Radar runs with `--auth-mode proxy`, also set:
  ```bash
  export RADAR_AUTH_MODE=proxy
  export RADAR_FORWARDED_USER=opensre
  export RADAR_FORWARDED_GROUPS=opensre-readonly
  ```
- The full-RCA smoke (`test_full_investigation_completes_with_radar_configured`)
  additionally needs an LLM provider key (e.g. `ANTHROPIC_API_KEY`); it skips
  otherwise.

## Run

```bash
RADAR_MCP_URL=http://localhost:9280/mcp \
  uv run python -m pytest tests/e2e/radar -m e2e -v
```

## Scenarios

| Test | Needs | Asserts |
| --- | --- | --- |
| `test_bridge_discovers_radar_read_tools` | Radar | The live `/mcp` server advertises the read tools we depend on |
| `test_targeted_tools_return_live_cluster_data` | Radar | `get_radar_dashboard` / `get_radar_issues` return real, parseable cluster data |
| `test_radar_resolves_into_effective_integrations` | Radar | `radar` surfaces as an available investigation source |
| `test_radar_tools_available_to_investigation_agent` | Radar | The agent's tool-availability path surfaces the radar tools |
| `test_full_investigation_completes_with_radar_configured` | Radar + LLM | A full investigation runs to completion with Radar configured |

## CI behavior

The whole `tests/e2e/` tree is excluded from the default offline suite
(`norecursedirs` in `pytest.ini`) and every test is marked `e2e`. These run only
where a live Radar (and, for the smoke, an LLM key) is provided. Each test skips
cleanly when its prerequisites are absent, so the suite is safe to invoke
anywhere.
