"""Ingestion — parse a user's own footprint into normalized, retrievable canonical items.

`base` defines the per-source adapter port + its raw output; `canonical` is the source-agnostic
normalized shape the attack/measure/defend engine consumes. The shared pipeline steps (normalize
here; third-party drop at M1.2; encrypt + embed + persist at M1.3) live in `services/ingestion.py`.
"""
