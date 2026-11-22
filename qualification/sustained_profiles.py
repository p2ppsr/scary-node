"""Immutable schedules and regtest endpoints for the native inventory cohorts."""

import os

DIRECT = "11e60074fbdde9587e1652d4de272a24bd4ac68b"
INVENTORY = "3ec961ef81881b83d4cc1ecc646250a2d8d200f9"
WITNESSED = "d2507fa8954f3da784316cd09e4ec2835ba806ea"
FUNDING = "3fb3a456448fd6d91801a35c04112ffab3f30dc0a6a14e426effff4f91660ba9"
ORIGINAL_IDS = "243170edb3df9bea289c32207732d988f6e4b92f67b17634a72e709ae8b735a2"
REORDERED_IDS = "03848032e833aa5bf7ed51366454149f0ac00dfae349d380a701f60c32728391"
ENDORSEMENT = bytes.fromhex(
    "00000020a90b66914fffff6e424ea1a6c00df3b3fa2f11045ca30118d9d68f4456a4b33f"
    "a860952bfbfb0ab5bca8d949096468e5eff0c156207276f9282fdb0edafaf9ead8e9016b"
    "ffff7f200000000001010000000100000000000000000000000000000000000000000000"
    "00000000000000000000ffffffff0302e501ffffffff0140be4025000000000151000000"
    "00"
)
ENDORSED_HASH = "7117895b33170a840833cd9965b06f383586e440384c695bbea881c4cc5c7ac7"
ENDORSED_SHA = "9cfde55565505d52e6b3c3e78e9811bb43745f0f3bddbb6c7798bddc199e5549"
COLD = "44e4d8eab6aab427453b23e1f7f098a9ba36d2fe"
FROZEN_RELEASE = "833fbeecc17a45de9edc8777123708ecd2e390d522c436b867a74a208318a528"
CHANGED_RELEASE = "4c9ef032f2ebf4c806331fbbbc53464c517da04cc497209bb9cdb5e4cc572faf"
RELEASE_REGISTRATION = "91444cdc0d304230b83d99138a07fda9b567d9f9"
RELEASE_ELF = "5129b3dea8cecf04d369aa7a7d663024879291799af5424cb0158e8e337b643e"
REORDERED_ARMS = ("reordered", "release-reordered")
WITNESSED_ARMS = ("endorsed", "reordered", "cold", "release-reordered")

BASELINE_BUILD = ("af9b605ad8da96d041a540111d46a7c0105209f7", FROZEN_RELEASE)
CHANGED_BUILDS = {
    "cold": ("c2a36d5c6a83f2262dffd596f5eecb421db6b132", CHANGED_RELEASE),
    "release-reordered": ("5148c31ae620544c31a61b1710172c4bcc524c54", RELEASE_ELF),
}
REGISTRATIONS = {
    "direct": DIRECT,
    "production": INVENTORY,
    "endorsed": WITNESSED,
    "reordered": WITNESSED,
    "cold": COLD,
    "release-reordered": RELEASE_REGISTRATION,
}

PROJECTS = {
    "direct": ("scary-node-throughput", 2000),
    "production": ("scary-node-sustained-production", 3000),
    "live-cadence": ("scary-node-live-cadence", 4000),
    "endorsed": ("scary-node-sustained-endorsed", 5000),
    "reordered": ("scary-node-sustained-reordered", 6000),
    "cold": ("scary-node-sustained-cold", 7000),
    "release-reordered": ("scary-node-sustained-release-reordered", 8000),
}


def profile():
    return os.getenv("SCARY_SUSTAINED_PROFILE", "direct")


def pinned_build(arm):
    return CHANGED_BUILDS.get(arm, BASELINE_BUILD)


def batch_order(arm):
    if arm not in REORDERED_ARMS:
        return range(10800)
    return [
        *range(1024),
        *(
            base + generation * 128 + quarter * 32 + local
            for base in range(1024, 10752, 512)
            for quarter in range(4)
            for generation in range(4)
            for local in range(32)
        ),
        *range(10752, 10800),
    ]


def cohort_by_batch(arm):
    cohorts = [0] * 10800
    for position, batch in enumerate(batch_order(arm)):
        cohorts[batch] = position // 1800
    return cohorts
