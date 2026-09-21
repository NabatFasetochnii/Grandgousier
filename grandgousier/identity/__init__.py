"""Stable physical-observation identity helpers.

The P4a layer deliberately does not change legacy ``frames.frame_uid`` semantics.
It only provides a path-independent identity for one physical exposure so several
file products may point to the same observation row.
"""

from .observation import (
    MASTER_IDENTITY_SCHEME,
    ROBOPHOT_IDENTITY_SCHEME,
    ObservationIdentity,
    ObservationIdentityError,
    ObservationIdentityMissing,
    build_frame_observation_identity,
    canonicalize_robophot_date_obs_utc,
    make_observation_uid,
)

__all__ = [
    "MASTER_IDENTITY_SCHEME",
    "ROBOPHOT_IDENTITY_SCHEME",
    "ObservationIdentity",
    "ObservationIdentityError",
    "ObservationIdentityMissing",
    "build_frame_observation_identity",
    "canonicalize_robophot_date_obs_utc",
    "make_observation_uid",
]
