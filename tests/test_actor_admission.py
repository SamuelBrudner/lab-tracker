from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

from lab_tracker.actor_admission import ActorAdmission
from lab_tracker.artifact_resolution_admission import ArtifactResolutionAdmission
from lab_tracker.store_health_admission import (
    DEFAULT_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT,
    DEFAULT_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT,
    MAX_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT,
    StoreHealthAdmission,
)


def _concurrent_acquisitions(
    admission: ActorAdmission[UUID],
    actor_ids: list[UUID],
):
    start = threading.Barrier(len(actor_ids))

    def acquire(actor_id: UUID):
        start.wait()
        return admission.try_acquire(actor_id)

    with ThreadPoolExecutor(max_workers=len(actor_ids)) as executor:
        return list(executor.map(acquire, actor_ids))


def test_actor_admission_atomically_enforces_global_limit():
    admission = ActorAdmission[UUID](
        global_in_flight_limit=4,
        per_actor_in_flight_limit=4,
        max_global_in_flight_limit=16,
    )

    leases = _concurrent_acquisitions(admission, [uuid4() for _ in range(16)])

    admitted = [lease for lease in leases if lease is not None]
    assert len(admitted) == 4
    for lease in admitted:
        lease.release()


def test_actor_admission_atomically_enforces_per_actor_limit():
    admission = ActorAdmission[UUID](
        global_in_flight_limit=16,
        per_actor_in_flight_limit=3,
        max_global_in_flight_limit=16,
    )
    actor_id = uuid4()

    leases = _concurrent_acquisitions(admission, [actor_id] * 16)

    admitted = [lease for lease in leases if lease is not None]
    assert len(admitted) == 3
    for lease in admitted:
        lease.release()


def test_actor_admission_lease_release_is_idempotent():
    admission = ActorAdmission[UUID](
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
        max_global_in_flight_limit=1,
    )
    actor_id = uuid4()
    lease = admission.try_acquire(actor_id)
    assert lease is not None

    lease.release()
    lease.release()

    replacement = admission.try_acquire(actor_id)
    assert replacement is not None
    replacement.release()


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "global_in_flight_limit": True,
            "per_actor_in_flight_limit": 1,
            "max_global_in_flight_limit": 2,
        },
        {
            "global_in_flight_limit": 1,
            "per_actor_in_flight_limit": True,
            "max_global_in_flight_limit": 2,
        },
        {
            "global_in_flight_limit": 1,
            "per_actor_in_flight_limit": 1,
            "max_global_in_flight_limit": True,
        },
    ],
)
def test_actor_admission_rejects_boolean_limits(kwargs):
    with pytest.raises(ValueError, match="must be an integer"):
        ActorAdmission[UUID](**kwargs)


def test_store_health_admission_has_bounded_defaults_and_maximum():
    admission = StoreHealthAdmission()

    assert (
        admission.global_in_flight_limit
        == DEFAULT_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT
    )
    assert (
        admission.per_actor_in_flight_limit
        == DEFAULT_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT
    )
    with pytest.raises(ValueError, match="must be no greater than 16"):
        StoreHealthAdmission(
            global_in_flight_limit=MAX_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT + 1
        )


def test_artifact_and_store_health_admission_capacity_is_independent():
    actor_id = uuid4()
    artifact_admission = ArtifactResolutionAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    store_health_admission = StoreHealthAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )

    artifact_lease = artifact_admission.try_acquire(actor_id)
    assert artifact_lease is not None
    assert artifact_admission.try_acquire(actor_id) is None

    store_health_lease = store_health_admission.try_acquire(actor_id)
    assert store_health_lease is not None

    artifact_lease.release()
    store_health_lease.release()
