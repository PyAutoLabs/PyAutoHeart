"""Prove the arcticpy install actually clocks charge, not just that pip exited 0.

Run by .github/workflows/arcticpy-action.yml against the install-arcticpy
composite action. A build can succeed, import, and still be useless if the
compiled arctic extension is broken — so this clocks a single bright pixel and
checks the result has the four properties every CTI repo depends on.

Kept as a file rather than a heredoc inside the workflow so it can be run
locally against any environment:

    python .github/scripts/arcticpy_smoke.py
"""

import numpy as np
import arcticpy as ac


def main() -> None:
    # One bright pixel in an otherwise empty column. CTI drags charge out of it
    # and releases it into the pixels clocked after it.
    image = np.zeros((20, 1))
    image[10, 0] = 1000.0

    traps = [ac.TrapInstantCapture(density=10.0, release_timescale=2.0)]
    # NOTE: add_cti wants a CCD, not a CCDPhase — a bare CCDPhase raises
    # AttributeError on fraction_of_traps_per_phase. Likewise parallel_roe is
    # not optional in practice: omitting it raises on roe.dwell_times.
    ccd = ac.CCD(full_well_depth=1e5, well_notch_depth=0.0, well_fill_power=0.8)
    roe = ac.ROE()

    clocked = ac.add_cti(
        image,
        parallel_traps=traps,
        parallel_ccd=ccd,
        parallel_roe=roe,
        parallel_express=5,
    )

    trail = clocked[11:, 0]
    print(f"bright pixel  : {image[10, 0]} -> {clocked[10, 0]}")
    print(f"trail         : {trail[:5]}")
    print(f"total charge  : {image.sum()} -> {clocked.sum()}")

    # 1. Charge left the bright pixel.
    assert clocked[10, 0] < image[10, 0], "no charge was trapped out of the bright pixel"

    # 2. It reappeared behind it, in pixels that started empty.
    assert trail[0] > 0, "no trail behind the bright pixel"

    # 3. The trail decays — the signature of exponential trap release.
    assert trail[0] > trail[1] > trail[2], f"trail is not decaying: {trail[:3]}"

    # 4. Charge is conserved. arctic's express approximation is not exactly
    #    conserving (measured ~6e-4 relative at express=5), so this is a
    #    gross-error check, not a precision one.
    assert np.isclose(clocked.sum(), image.sum(), rtol=5e-3), (
        f"charge not conserved: {image.sum()} -> {clocked.sum()}"
    )

    print("OK: arcticpy produced a decaying CTI trail with charge conserved")


if __name__ == "__main__":
    main()
