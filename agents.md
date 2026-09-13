## Coding style

- Prefer clear, linear code that is easy to trace and assure.
- Optimise for readability, auditability, and confidence in correctness rather than abstraction or elegance.
- Do not extract logic into helper functions unless:
  - the logic is genuinely reused,
  - the function represents a meaningful business concept, or
  - extraction materially improves readability or testing.
- Avoid excessive decomposition into small functions.
- Prefer seeing the main processing flow in one place where practical.
- Keep transformations explicit and sequential.
- Avoid clever abstractions, unnecessary design patterns, and premature generalisation.
- For data-processing code, favour an assurance-style pipeline: input → validation → transformation → checks → output.
- Add explicit validation and reconciliation checks at important stages rather than relying on implicit correctness.