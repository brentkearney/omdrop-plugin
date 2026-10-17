# Experimental X25519-only receiver configuration

For the Rust receiver in omdrop-core, add this **boolean** to the existing
`~/.config/airdrop/config.toml` (or `$XDG_CONFIG_HOME/airdrop/config.toml`):

```toml
compact_tls = true
```

While a receiving window is active, restart the receiver:

```bash
systemctl --user restart airdrop-receiver.service
```

Configuration is read on receiver startup. Delete the key, or set it to `false`, then restart
to restore default group selection. Keep the other configuration keys intact.
No systemd override, fixed installation path, CLI flag or identity migration
is required. The Python fallback does not support this setting and rejects
it as an unknown key; remove it before switching back to that implementation.
This option requires the core build containing this change.

## Effects and costs

The option limits key exchange to X25519. A peer offering only P-256 or P-384
cannot connect, under TLS 1.2 or 1.3. A TLS 1.3 peer offering X25519 in its
supported groups but sending only another group's initial key share needs
a HelloRetryRequest and an additional round trip. A peer that does not offer
X25519 fails instead. X25519-only exchange provides no post-quantum key exchange.

The current Rust receiver uses rustls with ring, which already prefers
X25519 and has no hybrid post-quantum groups. **No smaller handshake or faster
transfer than that default is claimed.** The original experiment used Python
and OpenSSL: the reviewer's Python 3.14 / OpenSSL 3.6 loopback measurement was
1,828 server handshake bytes with a hybrid group versus 835 with X25519.
Those counts do not describe the Rust receiver. Restricting an OpenSSL
configuration that otherwise negotiates a hybrid group gives up that
post-quantum protection; the current Rust default has none to give up.

The selected identity, certificate chain, certificate verification policy,
Contacts Only checks and TLS 1.2/1.3 support are preserved. The minimum read
rate remains 65,536 bytes/s with the existing initial grace and idle limits.
There is no configurable minimum-rate override. Upload concurrency, body,
archive/member and free-disk limits remain in force. Nearby hostile senders
gain no longer upload window or additional upload slots from this option;
incompatible peers fail during the existing bounded handshake. No payload
buffer or new progress logging is added.

## Historical hardware evidence and remaining qualification

On 12 October 2026, a Dell XPS 9320 (AX211 PCI 8086:51f0, kernel
7.2.8-5-omarchy-bore, firmware 89.d2579d43.0) received a 402,031-byte JPEG
and 3,853,909-byte PNG with the earlier Python experiment's compact TLS and
lowered rate floor, an existing EC self-signed identity, a receiver-bound
BLE advertiser and the experimental stock-driver connected GO backend.
The latter upload took about 17 seconds. The peer was an iPhone 16 Pro on
iOS 26.6.2 (owner-reported), with Everyone mode on both ends. Normal Wi-Fi
remained on channel 36 alongside the channel-6 GO; all 1,200 gateway probes
passed. These are historical combined-setup results, not tests of this Rust
revision or evidence that either receiver option was needed.

The PNG arrived above the unchanged 64 KiB/s floor. A previous photo stalled
and returned HTTP 408 after 30 seconds without progress; lowering the rate
floor does not address an idle stall, so that option has been removed.
An XPS comparison with and without compact TLS, Rust hardware qualification,
broader hardware and Contacts Only qualification remain outstanding. The
automated tests use constructed identities and IPv6 loopback, never live radios.
