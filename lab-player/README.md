# Lab player workspace

This directory is an isolated scratch area for playback validation artifacts.

Structure:

- `playlists/` stores fetched manifests and symlinks to the latest capture.
- `reports/` stores command output and validation notes.
- `scripts/` stores helper scripts used for controlled playback checks.

Nothing here should contain production secrets. URLs, credentials, and device
strings are supplied at runtime through environment variables.
