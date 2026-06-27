# Rules — Security & Privacy

Non-negotiable for a privacy product. Govern docs + implementation. (≤200 lines.)

## OWASP Top 10 (2025) — know + design out
- **A01 Broken Access Control** (#1; SSRF folded in) — deny-by-default; authorize **every** request server-side; scope every tenant query (RLS + app check); no IDOR.
- **A02 Security Misconfiguration** (#2, up from #5) — harden defaults, disable debug/docs in prod, set security headers.
- **A03 Software Supply Chain Failures** (new/expanded) — pin + scan dependencies; trust your build/distribution.
- **A04 Cryptographic Failures** — encrypt sensitive data at rest/in transit; strong algorithms; manage keys.
- **A05 Injection** — parameterize queries; treat all input as data. For LLMs: **OWASP LLM01 prompt injection** — delimit/datamark untrusted content, instruction-data separation.
- **A06 Insecure Design** — threat-model; secure- and privacy-by-design from the start.
- **A07 Authentication Failures** — strong authn, MFA, secure sessions.
- **A08 Software/Data Integrity Failures** — verify integrity (signatures), trusted CI/CD.
- **A09 Logging & Alerting Failures** — log security events (no sensitive data), alert on them.
- **A10 Mishandling of Exceptional Conditions** (new) — handle errors deliberately; **fail closed, never open**.
- **Verify against OWASP ASVS 5.0** (the ~350 testable requirements) — pair it with the Top 10. *Why:* turns "be secure" into a checklist.

## Access control & input (OWASP cheat sheets)
- **Deny by default** — justify every grant; new functionality starts locked. **Allowlist input validation** as early as possible. **OIDC for authn, OAuth for authz.** Sessions: unique, unpredictable, server-generated.

## Authentication (NIST SP 800-63B)
- **Length over complexity** — support ≥64 chars; min 8 (15 if the sole factor); **don't mandate character classes**. **Screen against breached/common passwords. No periodic forced rotation** (only on known compromise). **Encourage MFA.** *Why:* the modern evidence-based guidance.

## Privacy by design (GDPR)
- **7 principles** — lawfulness/fairness/transparency · **purpose limitation** · **data minimization** · accuracy · **storage limitation** · integrity & confidentiality · **accountability**.
- **Art. 25 — data protection by design & by default** — bake privacy in from design; default to the most protective setting. *Why:* retrofitting privacy fails. (We embody this: self-audit-only, third-party-drop, process-then-discard option, crypto-shred.)
- **Lawful basis + consent** — no run without a valid consent row; explicit **Art. 9** consent for special categories. **DSAR + erasure** must work (here erasure = crypto-shred).

## Crypto & secrets
- **Encrypt sensitive data at rest** — per-user DEK (KMS-wrapped); keys as bound params; app role can't read key material. **Secrets in env/secret-manager, never in code/logs. Never log secrets, keys, or decrypted content. Least privilege everywhere.**

## Sources
- [OWASP Top 10:2025](https://owasp.org/Top10/2025/) · [OWASP ASVS 5.0](https://owasp.org/www-project-application-security-verification-standard/) · [OWASP Cheat Sheets](https://cheatsheetseries.owasp.org/) · [OWASP LLM Top 10](https://genai.owasp.org/) · [NIST SP 800-63B](https://pages.nist.gov/800-63-4/sp800-63b.html) · [GDPR Art. 25](https://gdpr-info.eu/art-25-gdpr/).
