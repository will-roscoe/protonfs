# Security Policy

## Supported versions

protonfs follows semantic versioning from 1.0.0. Security fixes are released for
the latest `1.x` release line.

| Version | Supported |
| --- | --- |
| 1.x (latest) | :white_check_mark: |
| < 1.0 | :x: |

Always run the latest release: `pip install --upgrade protonfs`.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Report privately using one of:

- GitHub's [private vulnerability reporting](https://github.com/will-roscoe/protonfs/security/advisories/new)
  (**Security** tab → **Report a vulnerability**), or
- email **protonfs.git@willroscoe.uk**.

Please include:

- a description of the vulnerability and its impact,
- the protonfs version (`protonfs --version`) and the proton-drive version
  (`protonfs upgrade --check` reports it),
- steps to reproduce, and any proof-of-concept,
- any suggested remediation, if you have one.

## What to expect

- **Acknowledgement** within 5 working days.
- An assessment and, for confirmed issues, a fix timeline communicated to you.
- Coordinated disclosure: we will agree a disclosure date with you and credit you
  in the advisory unless you prefer to remain anonymous.

## Scope

protonfs orchestrates the official `proton-drive` CLI and manages local state
under `.protonfs/`. Relevant areas include: the SHA-512 pinning and verification
of downloaded binaries, the keyring/Secret Service bootstrap, handling of session
material, and local file/index handling.

Vulnerabilities in Proton Drive or the upstream `proton-drive` CLI itself should
be reported to Proton via [their security programme](https://proton.me/security).
