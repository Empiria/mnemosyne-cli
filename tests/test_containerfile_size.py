"""GREEN test for SBR-3.2 image-size acceptance assertion.

Plan 33.3-00 (Wave 0) committed this file as a RED stub: it imported
``IMAGE_SIZE_TARGET_BYTES`` from a constants module that did not yet exist and
asserted a hard 4 GiB value.

Plan 33.3-05 (Wave 4) owns the GREEN gate. Task 05.2 measured the multi-stage
empiria-claude image at **4.87 GiB**. Per RESEARCH Open Question 3 the operator
selected outcome ``size-soft``: the soft 5 GiB target is accepted as the CI
gate and the hard 4 GiB bar is deferred to a follow-on phase (Phase 33.4
candidate — scion-claude base-image gcloud SDK strip).

This test now asserts the *accepted soft threshold* exposed by
``mnemosyne_cli.containers.constants`` — making it pass GREEN — and records the
deferred hard target as a separate, non-enforced constant.
"""

from __future__ import annotations


def test_image_size_target_constant_defined():
    """IMAGE_SIZE_TARGET_BYTES is the enforced soft 5 GiB CI threshold (size-soft)."""
    from mnemosyne_cli.containers.constants import IMAGE_SIZE_TARGET_BYTES

    assert IMAGE_SIZE_TARGET_BYTES == 5 * 1024 * 1024 * 1024  # 5 GiB soft target


def test_image_size_hard_target_recorded_as_deferred():
    """The aspirational hard 4 GiB target is recorded for traceability, not enforced.

    Phase 33.3 Plan 05 outcome ``size-soft`` deferred the hard 4 GiB bar to a
    follow-on phase. The constant is kept so the deferral is auditable.
    """
    from mnemosyne_cli.containers.constants import IMAGE_SIZE_HARD_TARGET_BYTES

    assert IMAGE_SIZE_HARD_TARGET_BYTES == 4 * 1024 * 1024 * 1024  # 4 GiB deferred


def test_soft_target_exceeds_hard_target():
    """The enforced soft target must be looser than the deferred hard target."""
    from mnemosyne_cli.containers.constants import (
        IMAGE_SIZE_HARD_TARGET_BYTES,
        IMAGE_SIZE_TARGET_BYTES,
    )

    assert IMAGE_SIZE_TARGET_BYTES > IMAGE_SIZE_HARD_TARGET_BYTES
