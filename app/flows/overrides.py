"""Pure merge/validate for brand response override packs (BP-1.2)."""

from __future__ import annotations

import copy

from pydantic import BaseModel

from app.engine.nlg import _SLOT_PATTERN
from app.schemas.flow import ResponseTemplate
from app.schemas.manifest import ReplyManifest
from app.schemas.overrides import BrandOverridePack, BrandVariant


class OverrideError(BaseModel):
    reply_id: str
    code: str
    detail: str


class OverrideValidationError(Exception):
    def __init__(self, errors: list[OverrideError]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} override validation error(s)")


def _slots_in_text(text: str) -> set[str]:
    return set(_SLOT_PATTERN.findall(text))


def _brand_variant_to_template(variant: BrandVariant) -> ResponseTemplate:
    return ResponseTemplate(
        text=variant.text,
        language=variant.language,
        tone_register=variant.tone_register,
    )


def validate_pack(pack: BrandOverridePack, manifest: ReplyManifest) -> list[OverrideError]:
    """Validate every override; return all errors (field-level UI reuse in BP-3.1)."""
    errors: list[OverrideError] = []

    for override in pack.overrides:
        entry = manifest.entries.get(override.reply_id)
        if entry is None:
            errors.append(
                OverrideError(
                    reply_id=override.reply_id,
                    code="unknown_reply_id",
                    detail=f"reply_id not in manifest: {override.reply_id}",
                )
            )
            continue

        if entry.is_mandatory and not override.enabled:
            errors.append(
                OverrideError(
                    reply_id=override.reply_id,
                    code="locked_disable",
                    detail="mandatory reply cannot be disabled",
                )
            )

        if entry.is_mandatory and override.replace and not override.variants:
            errors.append(
                OverrideError(
                    reply_id=override.reply_id,
                    code="locked_no_variant",
                    detail="mandatory reply cannot be replaced with an empty variant list",
                )
            )

        allowed_slots = set(entry.slots)
        for index, variant in enumerate(override.variants):
            if not variant.text.strip():
                errors.append(
                    OverrideError(
                        reply_id=override.reply_id,
                        code="empty_text",
                        detail=f"variant {index} text is empty",
                    )
                )
                continue

            found_slots = _slots_in_text(variant.text)
            for slot in sorted(found_slots - allowed_slots):
                errors.append(
                    OverrideError(
                        reply_id=override.reply_id,
                        code="unknown_slot",
                        detail=f"unknown slot {{{slot}}} in variant {index}",
                    )
                )
            for slot in sorted(allowed_slots - found_slots):
                errors.append(
                    OverrideError(
                        reply_id=override.reply_id,
                        code="missing_slot",
                        detail=f"required slot {{{slot}}} missing from variant {index}",
                    )
                )

    return errors


def merge_response_overrides(
    platform_responses: dict[str, list[ResponseTemplate]],
    pack: BrandOverridePack,
    manifest: ReplyManifest,
) -> dict[str, list[ResponseTemplate]]:
    """Merge brand variants onto platform responses; pure — no IO, no input mutation."""
    errors = validate_pack(pack, manifest)
    if errors:
        raise OverrideValidationError(errors)

    merged = copy.deepcopy(platform_responses)

    for override in pack.overrides:
        if override.reply_id not in manifest.entries:
            continue
        if not override.enabled or not override.variants:
            continue

        brand_templates = [_brand_variant_to_template(variant) for variant in override.variants]
        if override.replace:
            merged[override.reply_id] = brand_templates
        else:
            merged[override.reply_id] = list(merged.get(override.reply_id, [])) + brand_templates

    return merged
