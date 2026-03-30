# Security Policy

Spiral Autonomy Kernel is an experimental open-source runtime for bounded self-evolution. It should not be treated as a drop-in security-hardened production platform.

## Reporting

Report suspected vulnerabilities privately to the project maintainer before public disclosure.

## Scope notes

The project intentionally limits self-modification to declared paths. Expanding those boundaries without review increases risk substantially.

## Deployment notes

For higher-trust environments:

- run with a restricted filesystem view,
- review allowed mutation paths,
- keep operator-visible logs,
- test rollback on your actual deployment target,
- disable model-backed changes until you validate the prompt contracts.
