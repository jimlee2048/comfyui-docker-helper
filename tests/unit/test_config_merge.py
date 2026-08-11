"""Focused contracts for layered raw TOML document merging."""

from copy import deepcopy
from typing import Any, cast

import pytest

from comfyui_docker_helper.config.diagnostics import SourceReference
from comfyui_docker_helper.config.host_merge_policies import (
    HOST_CONFIG_MERGE_POLICIES,
)
from comfyui_docker_helper.config.merge import (
    MergedDocument,
    SourceDocument,
    merge_toml_documents,
)


def _merge(*documents: dict[str, Any]) -> dict[str, Any]:
    return _merge_result(*documents).document


def _merge_result(*documents: dict[str, Any]) -> MergedDocument:
    return merge_toml_documents(
        (
            SourceDocument(SourceReference(index, f"layer-{index}"), document)
            for index, document in enumerate(documents)
        ),
        policies=HOST_CONFIG_MERGE_POLICIES,
    )


def test_default_merge_recurses_mappings_and_replaces_scalars_and_sequences() -> None:
    base = {
        "system": {
            "workspace": "/workspace",
            "env": {"BASE": "1", "SHARED": "base"},
            "extra_packages": ["git", "curl"],
        }
    }
    override = {
        "system": {
            "workspace": "/data",
            "env": {"SHARED": "override", "LATER": "1"},
            "extra_packages": ["jq"],
        }
    }

    assert _merge(base, override) == {
        "system": {
            "workspace": "/data",
            "env": {"BASE": "1", "SHARED": "override", "LATER": "1"},
            "extra_packages": ["jq"],
        }
    }


def test_empty_sequence_resets_an_ordinary_or_keyed_sequence() -> None:
    base = {
        "build": {"platforms": ["linux/amd64"]},
        "files": [
            {
                "url": "https://example.com/model.bin",
                "dir": "models",
                "filename": "model.bin",
            }
        ],
    }

    assert _merge(base, {"build": {"platforms": []}, "files": []}) == {
        "build": {"platforms": []},
        "files": [],
    }


def test_secret_tables_replace_atomically_without_inheriting_fields() -> None:
    result = _merge(
        {
            "secrets": {
                "shared": {"env": "TOKEN"},
                "retained": {"file": "/run/secrets/retained"},
            }
        },
        {"secrets": {"shared": {"file": "tokens/shared"}}},
    )

    assert result["secrets"] == {
        "shared": {"file": "tokens/shared"},
        "retained": {"file": "/run/secrets/retained"},
    }


def test_unique_custom_node_overlay_is_recursive_and_keeps_first_slot() -> None:
    result = _merge(
        {
            "comfyui": {
                "custom_nodes": [
                    {
                        "type": "registry",
                        "id": "example-node",
                        "version": "1.0.0",
                        "pre_install_hooks": ["base-pre.sh"],
                        "post_install_hooks": ["base-post.sh"],
                    },
                    {
                        "type": "git",
                        "url": "https://example.com/retained.git",
                        "ref": "main",
                    },
                ]
            }
        },
        {
            "comfyui": {
                "custom_nodes": [
                    {
                        "type": "registry",
                        "id": "example-node",
                        "version": "2.0.0",
                        "pre_install_hooks": ["later-pre.sh"],
                    },
                    {
                        "type": "git",
                        "url": "https://example.com/appended.git",
                        "ref": "v1",
                    },
                ]
            }
        },
    )

    assert result["comfyui"]["custom_nodes"] == [
        {
            "type": "registry",
            "id": "example-node",
            "version": "2.0.0",
            "pre_install_hooks": ["later-pre.sh"],
            "post_install_hooks": ["base-post.sh"],
        },
        {
            "type": "git",
            "url": "https://example.com/retained.git",
            "ref": "main",
        },
        {
            "type": "git",
            "url": "https://example.com/appended.git",
            "ref": "v1",
        },
    ]


def test_unique_file_overlay_recurses_across_three_layers() -> None:
    merged = _merge_result(
        {
            "files": [
                {
                    "url": "https://example.com/base.bin",
                    "dir": "models",
                    "filename": "model.bin",
                    "overwrite": False,
                }
            ]
        },
        {
            "files": [
                {
                    "dir": "models",
                    "filename": "model.bin",
                    "overwrite": True,
                }
            ]
        },
        {
            "files": [
                {
                    "dir": "models",
                    "filename": "model.bin",
                    "downloader": "httpx",
                }
            ]
        },
    )
    result = merged.document

    assert result["files"] == [
        {
            "url": "https://example.com/base.bin",
            "dir": "models",
            "filename": "model.bin",
            "overwrite": True,
            "downloader": "httpx",
        }
    ]
    assert [
        merged.origins.exact_location(("files", 0, field)).source.layer_ordinal
        for field in ("url", "overwrite", "downloader")
    ] == [0, 1, 2]


def test_git_credential_overlay_is_atomic_and_does_not_inherit_password() -> None:
    result = _merge(
        {
            "cdh": {
                "git": {
                    "credentials": [
                        {
                            "match": "https://github.com/acme/",
                            "username": "base-user",
                            "password": {"secret": "base"},
                        },
                        {
                            "match": "https://gitlab.example.com/team/",
                            "username": "retained",
                        },
                    ]
                }
            }
        },
        {
            "cdh": {
                "git": {
                    "credentials": [
                        {
                            "match": "https://github.com/acme/",
                            "username": "later-user",
                        }
                    ]
                }
            }
        },
    )

    assert result["cdh"]["git"]["credentials"] == [
        {"match": "https://github.com/acme/", "username": "later-user"},
        {"match": "https://gitlab.example.com/team/", "username": "retained"},
    ]


def test_invalid_unkeyed_and_ambiguous_keyed_items_remain_visible() -> None:
    duplicate = {
        "type": "registry",
        "id": "duplicate",
        "version": "1.0.0",
    }
    merged = _merge_result(
        {"comfyui": {"custom_nodes": [duplicate, duplicate, "invalid"]}},
        {
            "comfyui": {
                "custom_nodes": [
                    {
                        "type": "registry",
                        "id": "duplicate",
                        "version": "2.0.0",
                    },
                    {"type": "registry", "version": "missing-id"},
                ]
            }
        },
    )
    result = merged.document

    assert result["comfyui"]["custom_nodes"] == [
        duplicate,
        duplicate,
        "invalid",
        {"type": "registry", "id": "duplicate", "version": "2.0.0"},
        {"type": "registry", "version": "missing-id"},
    ]
    locations = [
        merged.origins.exact_location(("comfyui", "custom_nodes", index))
        for index in range(5)
    ]
    assert all(location is not None for location in locations)
    assert [location.source.layer_ordinal for location in locations if location] == [
        0,
        0,
        0,
        1,
        1,
    ]


def test_duplicate_identities_in_the_incoming_layer_are_all_retained() -> None:
    base = {"type": "registry", "id": "duplicate", "version": "1.0.0"}
    later_one = {"type": "registry", "id": "duplicate", "version": "2.0.0"}
    later_two = {"type": "registry", "id": "duplicate", "version": "3.0.0"}

    merged = _merge_result(
        {"comfyui": {"custom_nodes": [base]}},
        {"comfyui": {"custom_nodes": [later_one, later_two]}},
    )

    assert merged.document["comfyui"]["custom_nodes"] == [
        base,
        later_one,
        later_two,
    ]
    assert [
        merged.origins.exact_location(("comfyui", "custom_nodes", index)).path
        for index in range(3)
    ] == [
        ("comfyui", "custom_nodes", 0),
        ("comfyui", "custom_nodes", 0),
        ("comfyui", "custom_nodes", 1),
    ]


def test_merge_does_not_mutate_or_alias_inputs_or_prior_results() -> None:
    base = {"system": {"env": {"BASE": "1"}}, "build": {"tags": ["base"]}}
    override = {"system": {"env": {"LATER": "1"}}}
    original_base = deepcopy(base)
    original_override = deepcopy(override)

    result = _merge(base, override)
    assert base == original_base
    assert override == original_override

    result["system"]["env"]["BASE"] = "changed"
    result["build"]["tags"].append("changed")
    override["system"]["env"]["LATER"] = "mutated-after-merge"

    assert base == original_base
    assert result["system"]["env"]["LATER"] == "1"

    prior_result = _merge(base)
    next_result = _merge(prior_result, override)
    next_result["system"]["env"]["BASE"] = "next-result-only"
    assert prior_result["system"]["env"]["BASE"] == "1"


def test_source_identity_depends_on_layer_ordinal_not_display_label() -> None:
    assert SourceReference(2, "first-label") == SourceReference(2, "other-label")


def test_recursive_merge_tracks_leaf_owners_and_mixed_contributors() -> None:
    merged = _merge_result(
        {"system": {"workspace": "/workspace", "env": {"BASE": "1"}}},
        {"system": {"workspace": "/data", "env": {"LATER": "1"}}},
    )

    system = merged.origins.node_at(("system",))
    assert system is not None
    assert system.authored_at is None
    assert [location.source.layer_ordinal for location in system.contributors] == [0, 1]
    retained = merged.origins.exact_location(("system", "env", "BASE"))
    replaced = merged.origins.exact_location(("system", "workspace"))
    assert retained is not None
    assert replaced is not None
    assert retained.source == SourceReference(0, "layer-0")
    assert replaced.source == SourceReference(1, "layer-1")


def test_keyed_origins_keep_authored_indexes_separate_from_effective_indexes() -> None:
    merged = _merge_result(
        {
            "files": [
                {
                    "url": "https://example.com/base.bin",
                    "dir": "models",
                    "filename": "model.bin",
                    "overwrite": False,
                }
            ]
        },
        {
            "files": [
                {
                    "url": "https://example.com/new.bin",
                    "dir": "models",
                    "filename": "new.bin",
                },
                {
                    "dir": "models",
                    "filename": "model.bin",
                    "overwrite": True,
                },
            ]
        },
    )

    retained_url = merged.origins.exact_location(("files", 0, "url"))
    overlaid_value = merged.origins.exact_location(("files", 0, "overwrite"))
    appended_url = merged.origins.exact_location(("files", 1, "url"))
    assert retained_url is not None and retained_url.path == ("files", 0, "url")
    assert overlaid_value is not None and overlaid_value.path == (
        "files",
        1,
        "overwrite",
    )
    assert appended_url is not None and appended_url.path == ("files", 0, "url")
    assert retained_url.source.layer_ordinal == 0
    assert overlaid_value.source.layer_ordinal == 1
    assert appended_url.source.layer_ordinal == 1
    item = merged.origins.node_at(("files", 0))
    assert item is not None
    assert [location.path for location in item.contributors] == [
        ("files", 0),
        ("files", 1),
    ]


def test_reset_and_atomic_replacement_own_missing_field_attribution() -> None:
    base = {"files": [{"dir": "models", "filename": "model.bin"}]}
    reset = _merge_result(base, {"files": []})
    reset_location = reset.origins.exact_location(("files",))
    assert reset_location is not None
    assert reset_location.source.layer_ordinal == 1
    reset_node = reset.origins.node_at(("files",))
    assert reset_node is not None and reset_node.children == {}

    reset_then_append = _merge_result(
        base,
        {"files": []},
        {
            "files": [
                {
                    "url": "https://example.com/later.bin",
                    "dir": "models",
                    "filename": "later.bin",
                }
            ]
        },
    )
    appended_location = reset_then_append.origins.exact_location(("files", 0))
    assert appended_location is not None
    assert appended_location.source.layer_ordinal == 2

    credential = _merge_result(
        {
            "cdh": {
                "git": {
                    "credentials": [
                        {
                            "match": "https://github.com/acme/",
                            "username": "base",
                            "password": {"secret": "base"},
                        }
                    ]
                }
            }
        },
        {
            "cdh": {
                "git": {
                    "credentials": [
                        {
                            "match": "https://github.com/acme/",
                            "username": "later",
                        }
                    ]
                }
            }
        },
    )
    path = ("cdh", "git", "credentials", 0, "password")
    assert credential.origins.exact_location(path) is None
    owner = credential.origins.missing_field_location(path)
    assert owner is not None
    assert owner.source.layer_ordinal == 1
    assert owner.path == ("cdh", "git", "credentials", 0)


def test_recursive_mixed_container_does_not_invent_a_missing_field_owner() -> None:
    merged = _merge_result(
        {
            "comfyui": {
                "custom_nodes": [
                    {"type": "registry", "id": "example-node", "version": "1"}
                ]
            }
        },
        {
            "comfyui": {
                "custom_nodes": [
                    {"type": "registry", "id": "example-node", "version": "2"}
                ]
            }
        },
    )

    assert (
        merged.origins.missing_field_location(("comfyui", "custom_nodes", 0, "missing"))
        is None
    )
    assert (
        merged.origins.missing_field_location(
            ("comfyui", "custom_nodes", 0, "version", "invalid-child")
        )
        is None
    )


def test_ordinary_sequence_and_secret_atomic_origins_use_the_later_source() -> None:
    merged = _merge_result(
        {
            "build": {"tags": ["base"]},
            "secrets": {"shared": {"env": "BASE_TOKEN"}},
        },
        {
            "build": {"tags": ["later"]},
            "secrets": {"shared": {"file": "tokens/later"}},
        },
    )

    tags = merged.origins.exact_location(("build", "tags"))
    secret = merged.origins.exact_location(("secrets", "shared"))
    assert tags is not None and tags.source.layer_ordinal == 1
    assert secret is not None and secret.source.layer_ordinal == 1
    assert secret.path == ("secrets", "shared")


def test_origin_children_are_deeply_immutable() -> None:
    origins = _merge_result({"system": {"workspace": "/workspace"}}).origins

    with pytest.raises(TypeError):
        cast(Any, origins.children)["system"] = origins
    with pytest.raises(TypeError):
        cast(Any, origins.children["system"].children)["workspace"] = origins


def test_empty_mapping_keeps_an_authored_container_origin() -> None:
    origins = _merge_result({"system": {}}).origins

    system = origins.node_at(("system",))
    assert system is not None
    assert system.authored_at is not None
    assert system.authored_at.path == ("system",)
    assert system.children == {}
