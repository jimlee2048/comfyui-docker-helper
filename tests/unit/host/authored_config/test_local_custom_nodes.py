"""Authored local custom-node validation and layered intent."""

from copy import deepcopy

import pytest

from comfyui_docker_helper.config.authored.merge_policies import (
    HOST_CONFIG_MERGE_POLICIES,
)
from comfyui_docker_helper.config.authored.validation.domains import (
    validate_final_config_domains,
)
from comfyui_docker_helper.config.authored.validation.result import FinalConfigError
from comfyui_docker_helper.config.authored.validation.semantics import (
    validate_final_config_semantics,
)
from comfyui_docker_helper.config.authored.validation.structure import (
    validate_final_config_structure,
)
from comfyui_docker_helper.config.diagnostics import SourceReference
from comfyui_docker_helper.config.merge import SourceDocument, merge_toml_documents
from tests.build_plan_support import final_config


def document():
    value = final_config().model_dump(mode="json")
    value["comfyui"]["custom_nodes"] = [
        dict(type="local", source="/nonexistent/source", target_dir="local-node")
    ]
    return value


def test_local_validate_is_source_lazy_and_does_not_require_manager():
    value = document()
    value["comfyui"]["install_manager"] = False
    value["comfyui"]["install_cli"] = False
    value["files"] = [
        dict(
            type="local",
            source="/nonexistent/overlay",
            target="custom_nodes/local-node",
        )
    ]
    config = validate_final_config_structure(value)
    domains = validate_final_config_domains(config)
    assert not domains.diagnostics
    assert not validate_final_config_semantics(config, domains)
    assert config.comfyui.custom_nodes[0].content_lock is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", ""),
        ("source", "  "),
        ("target_dir", "../escape"),
        ("target_dir", ".wh.hidden"),
        ("target_dir", ".cdh-staging"),
    ],
)
def test_local_domains_reject_invalid_values(field, value):
    raw = document()
    raw["comfyui"]["custom_nodes"][0][field] = value
    domains = validate_final_config_domains(validate_final_config_structure(raw))
    assert any(
        item.path == ("comfyui", "custom_nodes", 0, field)
        for item in domains.diagnostics
    )


@pytest.mark.parametrize("field", ["source", "target_dir"])
def test_local_fields_are_required(field):
    raw = document()
    del raw["comfyui"]["custom_nodes"][0][field]
    with pytest.raises(FinalConfigError):
        validate_final_config_structure(raw)


@pytest.mark.parametrize(
    "field,value",
    [("pre_clone_hooks", []), ("content_lock", "false")],
    ids=["pre-clone-hooks", "nonboolean-lock"],
)
def test_local_rejects_git_hooks_and_nonboolean_lock(field, value):
    raw = document()
    raw["comfyui"]["custom_nodes"][0][field] = value
    with pytest.raises(FinalConfigError):
        validate_final_config_structure(raw)


@pytest.mark.parametrize(
    "other",
    [
        dict(type="local", source="elsewhere", target_dir="local-node"),
        dict(type="git", url="https://example.com/node.git", target_dir="local-node"),
    ],
)
def test_known_node_targets_are_unique_across_sources(other):
    raw = document()
    raw["comfyui"]["custom_nodes"].append(other)
    config = validate_final_config_structure(raw)
    assert validate_final_config_semantics(
        config, validate_final_config_domains(config)
    )


def test_local_overlay_replaces_source_and_hooks_in_original_slot():
    first = document()
    first["comfyui"]["custom_nodes"][0]["pre_install_hooks"] = ["before.py"]
    later = deepcopy(first)
    later["comfyui"]["custom_nodes"][0].update(
        source="../new", pre_install_hooks=["after.py"]
    )
    later["comfyui"]["custom_nodes"].append(
        dict(type="local", source="../new", target_dir="second")
    )
    merged = merge_toml_documents(
        (
            SourceDocument(SourceReference(index, f"layer-{index}"), raw)
            for index, raw in enumerate((first, later))
        ),
        policies=HOST_CONFIG_MERGE_POLICIES,
    ).document["comfyui"]["custom_nodes"]
    assert [node["target_dir"] for node in merged] == ["local-node", "second"]
    assert merged[0]["source"] == "../new"
    assert merged[0]["pre_install_hooks"] == ["after.py"]
