"""BP-1.2: merge_response_overrides + validate_pack safety rules."""

import copy

import pytest

from app.engine.nlg import pick_variant_with_index
from app.flows.loader import load_all_flows
from app.flows.manifest import load_reply_manifest
from app.flows.overrides import (
    OverrideValidationError,
    merge_response_overrides,
    validate_pack,
)
from app.schemas.overrides import BrandOverridePack, BrandVariant, ReplyOverride


@pytest.fixture
def manifest():
    return load_reply_manifest()


@pytest.fixture
def platform_responses():
    return load_all_flows().responses


def _pack(*overrides: ReplyOverride, manifest_version: str = "1.0.0") -> BrandOverridePack:
    return BrandOverridePack(
        agent_id="agent-test",
        pack_id="pack-test",
        manifest_version=manifest_version,
        overrides=list(overrides),
    )


def test_unknown_reply_id(manifest):
    pack = _pack(
        ReplyOverride(
            reply_id="totally_fake_reply",
            variants=[BrandVariant(text="hello")],
        )
    )
    errors = validate_pack(pack, manifest)
    assert any(error.code == "unknown_reply_id" for error in errors)
    with pytest.raises(OverrideValidationError):
        merge_response_overrides({}, pack, manifest)


def test_mandatory_locked_disable(manifest):
    pack = _pack(
        ReplyOverride(
            reply_id="ask_identity_verification",
            enabled=False,
            variants=[BrandVariant(text="custom")],
        )
    )
    errors = validate_pack(pack, manifest)
    assert any(error.code == "locked_disable" for error in errors)


def test_mandatory_locked_no_variant(manifest):
    pack = _pack(
        ReplyOverride(
            reply_id="ask_identity_verification",
            replace=True,
            variants=[],
        )
    )
    errors = validate_pack(pack, manifest)
    assert any(error.code == "locked_no_variant" for error in errors)


def test_missing_slot(manifest):
    pack = _pack(
        ReplyOverride(
            reply_id="dues_breakup_detail",
            variants=[
                BrandVariant(text="Total {amount_due}: principal {principal}, interest {interest}.")
            ],
        )
    )
    errors = validate_pack(pack, manifest)
    assert any(error.code == "missing_slot" and "{charges}" in error.detail for error in errors)


def test_unknown_slot(manifest):
    pack = _pack(
        ReplyOverride(
            reply_id="confirm_ptp",
            variants=[BrandVariant(text="Pay {amount_due} on {ptp_date} ref {bogus_slot}")],
        )
    )
    errors = validate_pack(pack, manifest)
    assert any(error.code == "unknown_slot" and "bogus_slot" in error.detail for error in errors)


def test_empty_text(manifest):
    pack = _pack(
        ReplyOverride(
            reply_id="confirm_ptp",
            variants=[BrandVariant(text="   ")],
        )
    )
    errors = validate_pack(pack, manifest)
    assert any(error.code == "empty_text" for error in errors)


def test_validate_pack_returns_all_errors(manifest):
    pack = _pack(
        ReplyOverride(reply_id="not_in_manifest", variants=[BrandVariant(text="x")]),
        ReplyOverride(reply_id="confirm_ptp", variants=[BrandVariant(text="")]),
        ReplyOverride(
            reply_id="dues_breakup_detail",
            variants=[BrandVariant(text="only {amount_due}")],
        ),
    )
    errors = validate_pack(pack, manifest)
    codes = {error.code for error in errors}
    assert "unknown_reply_id" in codes
    assert "empty_text" in codes
    assert "missing_slot" in codes
    assert len(errors) >= 3


def test_happy_append_extends_rotation_pool(platform_responses, manifest):
    reply_id = "confirm_ptp"
    base_len = len(platform_responses[reply_id])
    pack = _pack(
        ReplyOverride(
            reply_id=reply_id,
            replace=False,
            variants=[
                BrandVariant(text="Brand A {ptp_date} {amount_due}", language="hi"),
                BrandVariant(text="Brand B {ptp_date} {amount_due}", language="en"),
            ],
        )
    )
    merged = merge_response_overrides(platform_responses, pack, manifest)
    assert len(merged[reply_id]) == base_len + 2

    merged_texts = [variant.text for variant in merged[reply_id]]
    assert "Brand A {ptp_date} {amount_due}" in merged_texts
    assert "Brand B {ptp_date} {amount_due}" in merged_texts

    hi_picks = [
        pick_variant_with_index(
            merged[reply_id],
            preferred_language="hi",
            rotation_index=i,
        )[0].text
        for i in range(4)
    ]
    assert "Brand A {ptp_date} {amount_due}" in hi_picks
    assert len(set(hi_picks)) >= 2


def test_happy_replace_swaps_pool(platform_responses, manifest):
    reply_id = "confirm_ptp"
    pack = _pack(
        ReplyOverride(
            reply_id=reply_id,
            replace=True,
            variants=[
                BrandVariant(
                    text="Replaced copy {ptp_date} pay {amount_due}",
                    language="hi",
                )
            ],
        )
    )
    merged = merge_response_overrides(platform_responses, pack, manifest)
    assert len(merged[reply_id]) == 1
    assert merged[reply_id][0].text == "Replaced copy {ptp_date} pay {amount_due}"
    assert "{ptp_date}" in merged[reply_id][0].text
    assert "{amount_due}" in merged[reply_id][0].text


def test_merge_does_not_mutate_platform_responses(platform_responses, manifest):
    snapshot = copy.deepcopy(platform_responses)
    original_lists = dict(platform_responses)

    pack = _pack(
        ReplyOverride(
            reply_id="confirm_ptp",
            replace=True,
            variants=[BrandVariant(text="Mutate check {ptp_date} {amount_due}", language="hi")],
        )
    )
    merge_response_overrides(platform_responses, pack, manifest)

    assert platform_responses == snapshot
    for reply_id, variants in platform_responses.items():
        assert variants is original_lists[reply_id]
