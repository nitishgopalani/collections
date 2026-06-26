"""Validated brand override packs for fixture/stub mode (BP-1.3)."""

from app.flows.manifest import MANIFEST_VERSION, load_reply_manifest
from app.flows.overrides import validate_pack
from app.schemas.overrides import BrandOverridePack, BrandVariant, ReplyOverride

_MANIFEST = load_reply_manifest()


def _validated(pack: BrandOverridePack) -> BrandOverridePack:
    errors = validate_pack(pack, _MANIFEST)
    if errors:
        codes = ", ".join(f"{error.reply_id}:{error.code}" for error in errors)
        raise ValueError(f"Invalid fixture override pack {pack.pack_id}: {codes}")
    return pack


PACK_APPEND_MINIMAL = _validated(
    BrandOverridePack(
        agent_id="agent-fixture-append",
        pack_id="pack-append-v1",
        manifest_version=MANIFEST_VERSION,
        overrides=[
            ReplyOverride(
                reply_id="confirm_ptp",
                replace=False,
                variants=[
                    BrandVariant(
                        text="Brand append line {ptp_date} total {amount_due}.",
                        language="hi",
                    )
                ],
            )
        ],
    )
)

PACK_HEAVIER_REPLACE = _validated(
    BrandOverridePack(
        agent_id="agent-fixture-heavy",
        pack_id="pack-heavy-v1",
        manifest_version=MANIFEST_VERSION,
        overrides=[
            ReplyOverride(
                reply_id="confirm_ptp",
                replace=True,
                variants=[
                    BrandVariant(
                        text="Heavy replace {ptp_date} pay {amount_due} please.",
                        language="hi",
                    ),
                    BrandVariant(
                        text="Heavy replace EN {ptp_date} pay {amount_due}.",
                        language="en",
                    ),
                ],
            ),
            ReplyOverride(
                reply_id="ask_ptp_date",
                replace=False,
                variants=[
                    BrandVariant(
                        text="Kab payment kar sakte hain? Date batayein.",
                        language="hi",
                    )
                ],
            ),
        ],
    )
)

FIXTURE_PACKS_BY_AGENT: dict[str, BrandOverridePack] = {
    PACK_APPEND_MINIMAL.agent_id: PACK_APPEND_MINIMAL,
    PACK_HEAVIER_REPLACE.agent_id: PACK_HEAVIER_REPLACE,
}
