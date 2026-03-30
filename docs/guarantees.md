# Guarantees

## What the project tries to guarantee

- durable state across runs,
- offline fallback behavior when no model API key is configured,
- bounded code mutation paths via policy checks,
- snapshot and restore support for repository changes,
- explicit event logging for runtime decisions.

## What it does not guarantee

- correctness of generated changes,
- safety outside the declared mutation boundary,
- task success without meaningful evaluation signals,
- full production hardening for every environment.
