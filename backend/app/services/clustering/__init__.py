"""Clustering heuristics (P8.8). Each heuristic is a SEPARATE, named, versioned producer that writes its
own ``source_query`` (Invariant #3) and per-membership confidence, reusing the entity/merge/retraction
spine so it is REVERSIBLE by construction and produces SIDE-BY-SIDE cluster claims (never merged into one
answer — Invariant #4). Conservative defaults: co-spend stays ON; every new heuristic defaults OFF.

  * ``btc_change``  — BlockSci 0.7 change-address heuristics (faithful) + composition + require-N-agree.
  * ``evm``         — Victor 2020 deposit-address reuse / airdrop multi-participation / self-authorization.
  * ``community``   — Leiden (Traag 2019) community detection — VISUAL STRUCTURE only, never an ownership
                      claim, never written as an entity_membership.
  * ``service``     — apply / preview / undo orchestration (undo a heuristic run as a unit).
"""
