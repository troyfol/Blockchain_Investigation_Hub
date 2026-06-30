"""Application configuration (CLAUDE.md §3, phase_00 step 3).

Holds connector enable/disable + base URLs + paid-tier flags, the cache TTL, and the
**per-chain finality thresholds** that drive Invariant #6 (finality before immutability).

API keys never live here — see ``secrets.py`` (OS keyring, with one loud env opt-in).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Per-chain finality thresholds (docs/schema.md §2) ----------------------------
#
# `confirmations >= threshold(chain)` flips a transaction from `provisional` to `final`. Confirmed
# against live sources 2026-06-28 (docs/findings/external_facts_confirmation.md). These are POLICY
# KNOBS that intentionally err HIGH — Invariant #6: never freeze tip data as `final` prematurely.
# Override via BIH_FINALITY_THRESHOLDS (JSON). Keyed by lowercase chain name; EVM chainids resolve
# through CHAINID_TO_NAME.
DEFAULT_FINALITY_THRESHOLDS: dict[str, int] = {
    "bitcoin": 6,        # settled convention
    "ethereum": 64,      # chainid 1 — ~2 epochs == the consensus `finalized` checkpoint
    # --- L2 rollups: TRUE finality is L1 settlement, NOT an L2 block count. 20 is a conservative soft
    #     proxy until an L2 "finalized" flag keys off the L1 batch being finalized (a later enhancement).
    "arbitrum": 20,
    "optimism": 20,
    "base": 20,
    # polygon: Heimdall v2 (Jul 2025) milestones give deterministic finality in ~2-5s, reorgs <=2 blocks
    # — the old "PoS checkpoints are large" rationale is STALE. 128 is now far conservative; kept (errs
    # high, Invariant #6) but could be lowered to ~16-32 as a policy choice.
    "polygon": 128,
    # bsc: BEP-126 fast finality — final once two continuous blocks are justified (~2 blocks; recent
    # upgrades put wall-clock finality near 0.65s). 15 is a conservative safe margin (TODO closed).
    "bsc": 15,
}

# EVM chainid -> canonical chain name used as the finality-threshold key.
CHAINID_TO_NAME: dict[int, str] = {
    1: "ethereum",
    56: "bsc",           # BNB Smart Chain (Etherscan V2 chainid 56)
    42161: "arbitrum",
    10: "optimism",
    8453: "base",
    137: "polygon",
}

# Fallback when a chain is unknown: be conservative (use the strictest common EVM value).
DEFAULT_THRESHOLD_FALLBACK = 64


class Settings(BaseSettings):
    """App settings. Env vars are prefixed ``BIH_`` (e.g. ``BIH_ETHERSCAN_ENABLED=0``)."""

    model_config = SettingsConfigDict(
        env_prefix="BIH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Connectors: EVM (Etherscan V2) ---
    etherscan_enabled: bool = True
    etherscan_base_url: str = "https://api.etherscan.io/v2/api"
    etherscan_paid_tier: bool = False

    # --- Connectors: Bitcoin (Esplora) ---
    esplora_enabled: bool = True
    esplora_base_url: str = "https://blockstream.info/api"

    # --- Connectors: Valuation (DeFiLlama) ---
    defillama_enabled: bool = True
    defillama_base_url: str = "https://coins.llama.fi"

    # --- Optional PAID connectors (the integration layer, docs/findings/paid_api_integrations.md) ---
    # All DISABLED by default: a paid source is selectable only when its `*_enabled` flag is on AND its
    # key is in the OS keyring (see secrets.py). They are side-by-side, never blocking the free baseline
    # (Etherscan/Esplora/DeFiLlama/GraphSense/OFAC) — Invariant #4. Keys live in the keyring, NOT here.
    bitquery_enabled: bool = False
    bitquery_base_url: str = "https://streaming.bitquery.io/graphql"   # V2 (OAuth2 Bearer)
    bitquery_v1_base_url: str = "https://graphql.bitquery.io"          # V1 fallback (X-API-KEY)
    bitquery_use_v1: bool = False                                      # default V2 Bearer

    misttrack_enabled: bool = False
    misttrack_base_url: str = "https://openapi.misttrack.io"

    arkham_api_enabled: bool = False
    arkham_api_base_url: str = "https://api.arkm.com"

    oklink_enabled: bool = False
    oklink_base_url: str = "https://www.oklink.com/api/v5/explorer/"

    # --- Secrets policy ---
    # The ONLY non-keyring path for secrets; loud opt-in (see secrets.py).
    allow_plaintext_keys: bool = False

    # --- Cache / policy knobs ---
    cache_ttl_days: int = 30

    # Per-chain finality thresholds. A BIH_FINALITY_THRESHOLDS (JSON) override is *merged
    # onto* the defaults (see validator) so a partial override like {"polygon": 200} cannot
    # silently drop the settled bitcoin=6 / ethereum=64 values — mislabeling those would
    # break Invariant #6 (finality before immutability).
    finality_thresholds: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_FINALITY_THRESHOLDS)
    )

    @field_validator("finality_thresholds", mode="after")
    @classmethod
    def _merge_finality_defaults(cls, value: dict[str, int]) -> dict[str, int]:
        # Lowercase keys for lookup consistency, then layer the override on the defaults.
        normalized = {k.lower(): v for k, v in value.items()}
        return {**DEFAULT_FINALITY_THRESHOLDS, **normalized}

    def finality_threshold(self, chain: str | int) -> int:
        """Confirmations required for `final` on ``chain`` (name or EVM chainid)."""
        if isinstance(chain, int):
            chain = CHAINID_TO_NAME.get(chain, str(chain))
        return self.finality_thresholds.get(chain.lower(), DEFAULT_THRESHOLD_FALLBACK)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached)."""
    return Settings()
