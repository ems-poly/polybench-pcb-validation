# Security policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** through GitHub's private vulnerability reporting: open the repository's **Security** tab, then **Advisories**, then **Report a vulnerability**.

Do not open a public issue, discussion or pull request for a vulnerability.

## What counts

- Code execution, path traversal or request forgery in the Polybench server or CLI, including a web page reaching the local server from the browser.
- A plug-in or the core making network calls it should not make, or leaking data.
- **Instrument safety faults:** any way to leave a supply, load or source energised after an abort, crash or timeout, or to bypass `safe_state`. These can damage boards and hurt people, so they are treated as vulnerabilities.
- Supply-chain problems in the releases or workflows.

The server is designed to run on the bench machine and listens on 127.0.0.1 only. Exposing it to a network is not a supported configuration; reports about it are still welcome.

## What to expect

This is a single-maintainer project. The aim is to acknowledge a report within 7 days and to agree a disclosure date with you. Fixes ship as a new release with a GitHub security advisory crediting you, unless you ask otherwise.

## Supported versions

Only the latest release receives security fixes.
