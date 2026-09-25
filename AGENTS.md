# Agent notes for omdrop-plugin

## Never use the work email

`brent@atvenu.com` is a work address and this is personal work. Do not put it in
a commit author, a `Signed-off-by:` trailer, a manifest, an issue, a PR, or any
file here. Use `1550934+brentkearney@users.noreply.github.com`.

Check before committing, because this repository had no identity configured at
all until 2026-09-21 and a wrong one was set in its place:

```bash
git config user.email     # expect the noreply address
```

If the operator names a different address explicitly, use that. Absent that,
the noreply address is the only correct answer.

Some pushed commits still carry the work address. Rewriting that history would
break the marketplace verification snapshot, so it has not been done — do not
rewrite it on your own initiative.

## Release order

The plugin forwards flags to `send-to-peer`, which ships with the driver
package, so the driver is released first and the pin moves with it:

1. Merge and release [omdrop-awdl](https://github.com/brentkearney/omdrop-awdl),
   tagging `vX.Y.Z`.
2. Update **both** driver pin sites in `bin/omdrop`: `DRIVER_COMMIT=` and the
   bare SHA inside the build script. `tests/test_pinned_dependencies.py`
   requires full 40-character SHAs used literally. Set `DRIVER_VERSION=` to
   that commit's `pkgver`: `install-driver` rebuilds any older installed driver.
3. Bump `manifest.json`.
4. Retarget the marketplace verification issue.

## Marketplace verification

Submitting or retargeting is done by **editing the issue body**, not by
commenting — only opening or editing runs the validation workflow. The target
commit must equal current repository HEAD, so any push to `main` while a request
is open invalidates it and the bot re-labels it `needs-fixes`.

Leave the standard-installation acknowledgment unchecked: first use builds the driver and installs `python-libarchive-c` with `pacman` in a terminal, which is manual setup.
