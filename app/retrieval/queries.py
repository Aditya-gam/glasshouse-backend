"""Per-attribute retrieval queries — the embedding-relevance signal (text-inference.md §3).

One attribute-flavoured query per target; the Retriever embeds each and takes the top-k similar
items, so attribute-specific signal isn't crowded out by a single global query. Pure data — these
are part of the engine, so changing them re-triggers benchmarking + calibration (hard-invalidation).
"""

from app.domain.output_schema import AttributeCode

ATTRIBUTE_QUERIES: dict[AttributeCode, str] = {
    "age": "this person's age, generation, life stage, school year or career timeline",
    "sex": "this person's gender or sex, pronouns, gendered experiences",
    "location": "where this person lives — city, region, neighborhood, local places, commute",
    "birthplace": "where this person was born or grew up, hometown, origin, first language",
    "occupation": "this person's job, profession, employer, workplace, industry, role, tools",
    "education": "this person's education, degree, university, field of study, academic level",
    "relationship": "this person's relationship or marital status, partner, family, dating life",
    "income": "this person's income, wealth, spending, financial situation, job seniority",
}
