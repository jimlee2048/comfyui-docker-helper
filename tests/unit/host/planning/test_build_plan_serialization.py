"""BuildPlan serialization, digest, and binding contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from tests.build_plan_support import (
    DIGEST_A,
    accepted_resolution,
    build_plan,
    final_config,
    request_graph,
)

from comfyui_docker_helper.config.authored.models import FinalConfig
from comfyui_docker_helper.config.planning.build_plan import (
    BuildPlan,
    ManifestBinding,
    build_plan_digest,
    dump_build_plan_json,
    manifest_binding,
    parse_build_plan_json,
)


def test_plan_bytes_digest_and_lock_order_are_deterministic() -> None:
    first = build_plan(final_config(), accepted_resolution())
    second = build_plan(final_config(), accepted_resolution(reverse=True))

    assert first == second
    assert dump_build_plan_json(first) == dump_build_plan_json(second)
    assert build_plan_digest(first) == build_plan_digest(second)


def test_plan_round_trip_is_strict_and_immutable() -> None:
    plan = build_plan(final_config(), accepted_resolution())

    assert parse_build_plan_json(dump_build_plan_json(plan)) == plan
    with pytest.raises(ValidationError, match="frozen"):
        plan.image_config_digest = DIGEST_A

    document = plan.model_dump(mode="json")
    document["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BuildPlan.model_validate(document)


def test_plan_and_manifest_bind_image_config_and_lock_without_requests() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    binding = manifest_binding(plan)
    serialized = dump_build_plan_json(plan)

    assert binding.build_plan_digest == build_plan_digest(plan)
    assert binding.image_config_digest == plan.image_config_digest
    assert binding.lock_digest == plan.lock_digest
    assert ManifestBinding.model_validate_json(binding.model_dump_json()) == binding
    assert b"request_digest" not in serialized
    assert b"config.lock" not in serialized
    assert b"host" not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    [("tags", ["example:changed"]), ("output", "push")],
)
def test_publication_only_config_change_does_not_change_image_plan(
    field: str, value: object
) -> None:
    config = final_config()
    changed_document = config.model_dump(mode="python")
    changed_document["build"][field] = value
    changed = FinalConfig.model_validate(changed_document)

    first = build_plan(config, accepted_resolution())
    second = build_plan(changed, accepted_resolution())

    assert request_graph(config, accepted_resolution()) == request_graph(
        changed, accepted_resolution()
    )
    assert first == second
    assert dump_build_plan_json(first) == dump_build_plan_json(second)


def test_image_config_change_updates_binding_deterministically() -> None:
    config = final_config()
    changed_document = config.model_dump(mode="python")
    changed_document["system"]["env"]["IMAGE_INPUT"] = "changed"
    changed = FinalConfig.model_validate(changed_document)

    first = build_plan(config, accepted_resolution())
    second = build_plan(changed, accepted_resolution())

    assert first.image_config_digest != second.image_config_digest
    assert first.lock_digest == second.lock_digest
    assert build_plan_digest(first) != build_plan_digest(second)


@pytest.mark.parametrize("size", [20971520, "20971520", "20m", "20MiB"])
def test_equivalent_log_sizes_preserve_canonical_intent_and_plan(size: object) -> None:
    config = final_config()
    document = config.model_dump(mode="python")
    document["cdh"]["logs"]["max_size"] = size
    equivalent = FinalConfig.model_validate(document)
    accepted = accepted_resolution()
    assert request_graph(config, accepted) == request_graph(equivalent, accepted)
    assert dump_build_plan_json(build_plan(config, accepted)) == dump_build_plan_json(
        build_plan(equivalent, accepted)
    )


def test_log_settings_change_image_intent_and_are_strict_in_serialized_plan() -> None:
    config = final_config()
    document = config.model_dump(mode="python")
    document["cdh"]["logs"]["mode"] = "memory"
    changed = FinalConfig.model_validate(document)
    accepted = accepted_resolution()
    first = build_plan(config, accepted)
    second = build_plan(changed, accepted)
    assert first.image_config_digest != second.image_config_digest
    assert second.runtime.logs.mode == "memory"
    assert parse_build_plan_json(dump_build_plan_json(second)) == second
    with pytest.raises(ValidationError, match="frozen"):
        second.runtime.logs.max_size = 1
    serialized = second.model_dump(mode="json")
    serialized["runtime"]["logs"]["max_size"] = "20m"
    with pytest.raises(ValidationError):
        BuildPlan.model_validate_json(json.dumps(serialized))
