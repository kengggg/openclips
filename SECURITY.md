# Security

## What the pairing key protects

The 32-byte pairing key is the only credential the camera has. Anyone
holding it within Bluetooth range can control the camera and download
every photo on it. Treat `~/.config/openclips/pairings.json` like an SSH
private key. The CLI writes it with mode 0600 and never prints it.

If you think a key leaked: factory-reset the camera (pinhole 15 s), run
`openclips forget`, and pair again. That rotates the key.

## Threat model of the protocol itself

* Pairing is unauthenticated ECDH in a short window after a physical reset.
  An attacker in range during that window could pair instead of you. Pair
  somewhere quiet.
* The session is AES-EAX with per-session HKDF keys and fresh nonces, and
  the proofs bind both nonces. Replay of captured frames is not possible
  across sessions.
* The Wi-Fi SoftAP is WPA2 with a camera-generated passphrase that is only
  ever sent inside the encrypted BLE channel.

## Reporting a vulnerability in openclips

Please do not open a public issue for anything that would let a third party
read a user's photos or take over a camera. Email the maintainer listed in
`pyproject.toml` or use GitHub's private vulnerability reporting on the
repository. You should hear back within a week.
