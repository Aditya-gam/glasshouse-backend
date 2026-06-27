-- Extensions required by Glasshouse (run once on a fresh database).
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector: embeddings
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- field-level encryption
