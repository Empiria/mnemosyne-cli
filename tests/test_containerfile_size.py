"""RED test for SBR-3.2 image-size acceptance assertion.

Plan 33.3-01 / 33.3-05 (Wave 1 / Wave 4) define a hard 4 GiB target via
`IMAGE_SIZE_TARGET_BYTES` in a constants module (TBD by Wave 1). This file
is the Wave-0 RED stub — it imports the symbol and asserts the value.

Per RESEARCH Open Question 3 the planner is leaving this at HARD 4 GiB;
if Wave 4 measurement shows >4 GiB the orchestrator will surface a
checkpoint:decision.
"""

from __future__ import annotations


def test_image_size_target_constant_defined():
    """IMAGE_SIZE_TARGET_BYTES exposed by mnemosyne_cli.containers.constants must equal 4 GiB."""
    from mnemosyne_cli.containers.constants import IMAGE_SIZE_TARGET_BYTES
    assert IMAGE_SIZE_TARGET_BYTES == 4 * 1024 * 1024 * 1024  # 4 GiB
