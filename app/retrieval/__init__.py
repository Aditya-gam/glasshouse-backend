"""Retrieval — text embeddings (pgvector) and the evidence Retriever.

`embedder` owns the embedding port + its fastembed impl (ingest-time at M1.3, query-time at M1.6).
The high-recall Retriever (embedding ∪ recency ∪ always-include, token-capped) lands at M1.6.
"""
