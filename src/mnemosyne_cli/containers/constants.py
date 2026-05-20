"""Image-size acceptance thresholds for the empiria-claude capability image.

Phase 33.3 SBR-3.2 (D-10) set an aspirational hard target of < 4 GiB for the
empiria-claude image. RESEARCH Assumption A4 flagged that bar as aspirational:
~2.0 GiB of measured savings off the 6.93 GiB baseline lands at ~4.9 GiB worst
case.

Plan 05 Task 05.2 measured the multi-stage (builder + runtime) image at
**4.87 GiB**. Per RESEARCH Open Question 3, the operator selected the
``size-soft`` outcome: the soft 5 GiB target is accepted as the CI gate, and the
hard 4 GiB target is deferred to a follow-on phase (Phase 33.4 candidate —
scion-claude base-image gcloud SDK strip, residual ~0.87 GiB).

``IMAGE_SIZE_TARGET_BYTES`` is the *enforced* CI threshold — the soft 5 GiB
value. ``IMAGE_SIZE_HARD_TARGET_BYTES`` records the deferred aspirational bar
for traceability; it is not enforced in Phase 33.3.
"""

from __future__ import annotations

#: Soft acceptance threshold enforced by CI (publish-images.yml) as of
#: Phase 33.3 Plan 05, outcome ``size-soft``. 5 GiB = 5 * 1024**3 bytes.
IMAGE_SIZE_TARGET_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB (soft, enforced)

#: Aspirational hard target from SBR-3.2 D-10. Deferred to a follow-on phase
#: per RESEARCH Open Question 3 outcome ``size-soft``; NOT enforced in 33.3.
IMAGE_SIZE_HARD_TARGET_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB (deferred)
