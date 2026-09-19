# Omdrop

Send and receive files from nearby Apple devices over AirDrop on Apple Silicon devices running [Omarchy Linux](https://omarchy.org). 

https://github.com/user-attachments/assets/3e415609-6e25-48b0-b55f-58252c65356b

Depends on a [patched wifi driver](https://github.com/brentkearney/omdrop-awdl), which the plugin offers to install on first use.

Provides an Omarchy toolbar menu that:
- Toggles receiving mode on/off
- Sets the name of your device, as it appears to AirDrop users
- Sets the file download location (defaults to ~/Downloads)

Notification pops up when a file is received. Image or txt files get automatically copied to clipboard. Clicking the notification popup opens the file with the default app for the file type.

## Requirements

1. **Broadcom Wi-Fi whose firmware implements AWDL** — the link layer Apple that devices use to talk to each other directly. Verified on the BCM4387 (`14e4:4433`) in the MacBook Pro 16-inch, M1 Pro. Other Apple Broadcom parts are plausible and untested. Intel, MediaTek, and Qualcomm cards cannot do this. Apple models that ship with the BCM4387:
 - MacBook Pro 14" and 16", 2021 — M1 Pro / M1 Max (j314/j316, t600x)
 - Mac Studio, 2022 — M1 Max / M1 Ultra (j375)
 - MacBook Air 13", 2022 — M2 (j413)
 - MacBook Pro 13", 2022 — M2 (j493)
 - Mac mini, 2023 — M2 (j473) [INFERENCE]
2. **A `brcmfmac` kernel module that enables AWDL** — One is provided via the [omdrop-awdl package](https://github.com/brentkearney/omdrop-awdl), installed at first use of the plugin.
   
### Alternatives
If you want AirDrop on non-Apple hardware, look at [owl](https://github.com/seemoo-lab/owl) and [OpenDrop](https://github.com/seemoo-lab/opendrop) instead. They reimplement AWDL in userspace over monitor mode, which works on a different set of cards. Omdrop takes the opposite approach and drives the firmware's native AWDL implementation.

[LocalSend](https://localsend.org) is a cross-platform file sharing protocol that ships with Omarchy. It requires all devices (iPhone, etc) to install it; it is not compatible with native AirDrop, it is an open alternative to it.


## Install

```bash
omarchy plugin add https://github.com/brentkearney/omdrop-plugin.git
omarchy plugin enable netmojo.omdrop
```

The widget lands on the right of the bar. Move it with `omarchy bar move netmojo.omdrop --section center`.

On first use of the plugin, the panel opens a terminal to prompt for a password to install the [patched wifi driver](https://github.com/brentkearney/omdrop-awdl) and update your firewall rule to allow connections on the new virtual interface (TCP 8771 on `awdl0`, to/from an IPv6 link-local address). 

Both dependencies are pinned to exact revisions, so what gets built is the code this release was tested against: the driver at a named commit of [omdrop-awdl](https://github.com/brentkearney/omdrop-awdl), and the AirDrop support library from a named commit of the `opendrop` AUR recipe, whose source tarball `makepkg` verifies against a recorded checksum. Neither is installed by bare package name.

`opendrop` declares `owlink`, the userspace AWDL daemon its own sender uses. Omdrop drives AWDL in firmware and never runs it, so the library is installed with `--assume-installed owlink` rather than pulling in a package that could not be pinned.

`omdrop --version` reports what version you are running; include it in any bug report.

## Remove

```bash
omdrop firewall remove
omarchy plugin remove netmojo.omdrop
```
Remove the patched Wi-Fi driver separately with:
```
pacman -R brcmfmac-awdl-dkms
```

## Use

### Receiving

Click the parachute icon to open the panel. Inside:

- **The switch** turns receiving on and off. Right-clicking the bar icon does the same without opening the panel.
- **Stay visible for** sets how long receiving mode lasts, from one file to always on.
- **They see you as** sets the name of your device as others see it. Defaults to this machine's short hostname.
- **Save files to** sets the download folder. Defaults to `~/Downloads`.

A file that arrives is saved there, copied to the clipboard, and announced in a notification. Click the notification to open it in whatever application handles that file type. Nothing opens on its own.

Everything the panel does is also available from the command line:

```bash
omdrop on 10m        # visible to everyone for ten minutes
omdrop on once       # until one file arrives
omdrop status        # what is true right now
omdrop name "Study Mac"
omdrop dir ~/Drops
omdrop limit 30      # cap one transfer at 30% of currently free disk space
```

The receiver defaults to a maximum transfer size of 30% of the free space on
the download disk. Run `omdrop limit PERCENT` to set a value from 1 to 90. The
receiver also preserves at least 1 GiB of free space, even when the configured
percentage would allow a larger transfer.

### Sending

Sending works, and it is proven to both a Mac and an iPhone, but it is a command line at the moment: the panel has no send button yet. The tools come with the [driver package](https://github.com/brentkearney/omdrop-awdl), and they need a receive window open first, because that is what fills the peer table they read.

```bash
omdrop on 10m                                   # a window, so peers are registered
/usr/lib/omdrop/send-to-peer --list             # who is within earshot, and where
/usr/lib/omdrop/send-to-peer ~/photo.jpg        # the only peer heard
/usr/lib/omdrop/send-to-peer --mac e2:9d:.. ~/photo.jpg
/usr/lib/omdrop/send-to-peer --wait 120 ~/photo.jpg
```

Set the receiving Apple device to **Everyone**, or **Everyone for 10 Minutes** on iOS. Contacts Only is not supported: it rejects a self-signed certificate before any transfer starts. The recipient sees a prompt naming this computer and has to accept it, exactly as they would from an Apple device.

**A Mac** answers immediately, whether or not its Finder AirDrop window is open.

**An iPhone** only listens in short bursts, so a single attempt is a coin flip. `--wait` polls for the moment it starts listening and sends then. Opening a share sheet on the phone, or receiving anything on it, brings its listener up.


## Privileges

The radio helper runs as root through `pkexec`, because configuring AWDL means vendor command passthrough on the wireless interface and raw frame transmission. Neither is possible as an unprivileged user. The receiver itself runs as you, so received files are yours without a `chown`.

Privilege is granted by a named polkit action, `org.omarchy.omdrop.discover`, bound to one helper at `/usr/lib/omdrop/omdrop-discoverable`. That path is root-owned and not writable by the user whose session invokes it, which is the point: a rule that whitelists a script inside someone's home directory hands root to anything running as that user. Turning discoverability on from your own seat needs no password; a remote or inactive session must authenticate as an administrator.

The `brcmfmac-awdl-dkms` package installs both the helper and that action, so there is no manual privilege step and nothing here asks you to grant root to a script in your home directory.

The UFW exception belongs to the receiver, not the driver package. Omdrop runs `sudo ufw` with fixed arguments in the visible dependency-install terminal, after the package work. `sudo` normally reuses the authentication from installing the driver, so this does not cause a second password prompt. The rule is safe to apply repeatedly; UFW skips an identical existing rule.

## Problems and Contributions
If you encounter a bug, or have a feature request, [create an Issue](https://github.com/brentkearney/omdrop-plugin/issues) here. Or better yet, have your agent fix or implement it, and [create a Pull Request](https://github.com/brentkearney/omdrop-plugin/pulls). I'm happy to review and merge.

#### Known Bugs / Limitations
- No send button in the panel. Sending itself works from the command line, as [Use](#sending) describes; the UI for it is not built yet. PRs welcome, here for the panel or in [omdrop-awdl](https://github.com/brentkearney/omdrop-awdl/) for the sender.
- Sending to an iPhone needs `--wait`, because iOS only keeps an AirDrop listener up in short bursts. A Mac has no such quirk.

## Trademark
"AirDrop" is a trademark of Apple Inc. Omdrop is an independent project that is not affiliated with, authorized by, or endorsed by Apple.

## License

MIT. See [LICENSE](LICENSE).
