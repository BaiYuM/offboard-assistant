# Security Policy

## Privacy Boundary

Offboard Assistant is designed to track cleanup locations and metadata, not secrets.

It must not:

- Store plaintext passwords.
- Decrypt browser password values.
- Capture keyboard input.
- Read or upload chat contents.
- Upload plaintext reports or state files to cloud storage.
- Delete browser passwords, chat data, or files without explicit user review.

It may:

- Store browser login metadata such as origin URL, masked username, timestamps, and local database path.
- Store environment variable names without values.
- Store sensitive file paths without file contents.
- Store chat application data directory paths without message contents.
- Store installation metadata and paths.
- Upload encrypted sync bundles when the user provides a passphrase.

## Reporting Issues

Please report security issues privately if possible. Include:

- Affected version or commit.
- Platform and Python version.
- Reproduction steps.
- Whether any secret value, password, token, or chat content could be exposed.

## Handling Sensitive Test Data

Do not attach real browser databases, chat logs, `.env` files, API keys, or exported sync bundles to public issues.

