"""The section 11.1 wiring, as examples/wiring.py writes it, runs against the fake service."""

import runpy
from pathlib import Path

EXAMPLE = Path(__file__).parents[3] / "examples" / "wiring.py"


async def test_wiring_example_runs() -> None:
    namespace = runpy.run_path(str(EXAMPLE), run_name="example")
    result = await namespace["main"]()
    assert result["second_run_provider_requests"] == 0
    assert result["first_run_provider_requests"] == 2
