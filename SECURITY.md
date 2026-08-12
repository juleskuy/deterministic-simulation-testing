# Security Policy

## Supported versions

Only the latest version on `main` is supported. This project is stdlib-only and
ships no network client, but a simulator that misreports a result is a security
and correctness problem.

## Report a vulnerability

Do **not** open a public issue for vulnerabilities or exploit details.

Use GitHub's private security advisory flow for this repository:

1. Open the repository's **Security** tab.
2. Select **Report a vulnerability**.
3. Include the affected commit SHA, Python version, platform, minimal reproducer,
   observed result, and expected result.

If private reporting is unavailable, open a minimal issue that says only
"security report requested". Do not include exploit details, credentials, or
private system information; a private channel will be arranged.

## What counts as a security issue

Examples:

- A crafted input causes unbounded CPU, memory, or disk use.
- A malicious `SKILL.md`, journal, or source file can execute code outside the
  documented local scan/simulation scope.
- The scanner declares unsafe code clean due to an undocumented bypass.
- Replay, shrink, or documentation verification reports a result it did not
  actually reproduce, where users could rely on that claim for a safety-critical
  system.

Expected false positives from a lexical scanner are not vulnerabilities. Report
false negatives when they contradict a documented detection claim.

## Response targets

| Stage | Target |
|---|---:|
| Acknowledgement | 7 days |
| Initial assessment | 14 days |
| Fix or mitigation plan | 30 days |
| Public disclosure | after a fix is available, coordinated with reporter |

No bounty program is offered.

## Disclosure

Please allow a reasonable remediation window before public disclosure. Credit is
provided unless you request otherwise.
