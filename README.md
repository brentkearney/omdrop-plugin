# omdrop

Receive files from nearby Apple devices, from the Omarchy bar. Press the parachute, become visible for a bounded window, and what people send you lands in your Downloads folder.

**omdrop works only on Apple Silicon Macs, and needs a patched Wi-Fi driver.** Read [Requirements](#requirements) before installing. On any other machine the bar icon appears struck through and the controls stay hidden.

## What it does

- Makes this computer visible to nearby Apple devices for a window you choose: one file, a set number of minutes, or until you turn it off.
- Receives files into a folder you choose.
- Sets the name those devices show under your tile.

Nothing is sent. omdrop only receives.

## Requirements

omdrop is the interface. Three things have to exist underneath it, and this plugin installs none of them:

1. **Broadcom Wi-Fi whose firmware implements AWDL** — the link layer Apple devices use to talk to each other directly. Verified on the BCM4387 (`14e4:4433`) in the MacBook Pro 16-inch, M1 Pro. Other Apple Broadcom parts are plausible and untested. Intel, MediaTek, and Qualcomm cards cannot do this.
2. **A patched `brcmfmac`** exposing that firmware AWDL through vendor command passthrough. Not upstream, and rebuilt on every kernel upgrade — which is why it ships as a DKMS package rather than a file you copy.
3. **A radio helper and a receiver** on this machine, plus a polkit action letting the helper run without a password prompt.

The driver is a separate project: [omdrop-awdl](https://github.com/brentkearney/omdrop-awdl). Build and install it before this plugin, on Arch or Omarchy:

```bash
git clone https://github.com/brentkearney/omdrop-awdl.git
cd omdrop-awdl
makepkg -si
```

DKMS rebuilds it whenever a kernel is installed, and it fails toward stock Wi-Fi: the module lands in `updates/dkms/`, which `depmod` prefers over the in-tree driver without deleting it, so a build that breaks costs you AirDrop and never your Wi-Fi link.

If you want AirDrop on non-Apple hardware, look at [owl](https://github.com/seemoo-lab/owl) and [OpenDrop](https://github.com/seemoo-lab/opendrop) instead. They reimplement AWDL in userspace over monitor mode, which works on a different set of cards. omdrop takes the opposite approach and drives the firmware's own implementation.

## Install

```bash
omarchy plugin add https://github.com/brentkearney/omdrop-plugin.git
omarchy plugin enable netmojo.omdrop
```

The widget lands on the right of the bar. Move it with `omarchy bar move netmojo.omdrop --section center`.

`omdrop --version` reports what you are running; include it in any bug report.

## Remove

```bash
omarchy plugin remove netmojo.omdrop
```

That leaves the driver package alone. Remove it separately with `pacman -R brcmfmac-awdl-dkms`.

## Use

Click the parachute to open the panel. Inside:

- **The switch** turns receiving on and off. Right-clicking the bar icon does the same without opening the panel.
- **Stay visible for** sets how long a press lasts, from one file to always on.
- **They see you as** sets the name on your tile. Defaults to this machine's short hostname.
- **Save files to** sets the download folder. Defaults to `~/Downloads`.

A file that arrives is saved there, copied to the clipboard, and announced in a notification. Click the notification to open it in whatever application handles that file type. Nothing opens on its own.

Everything the panel does is also available from the command line:

```bash
omdrop on 10m        # visible to everyone for ten minutes
omdrop on once       # until one file arrives
omdrop status        # what is true right now
omdrop name "Study Mac"
omdrop dir ~/Drops
```

## What the panel tells you, and why it matters

The panel distinguishes three states that look alike but are not:

| It says | It means |
|---|---|
| Visible to everyone | Nearby devices can see you and send to you. |
| Nobody can see you yet | The receiver is running, but nothing is advertising on the radio. |
| Not available | The radio support is missing. omdrop cannot tell you anything. |

The middle state is the common failure. It means the service layer is healthy and the radio layer is not, which is worth knowing before you ask someone to send you a file.

## Privileges

The radio helper runs as root through `pkexec`, because configuring AWDL means vendor command passthrough on the wireless interface and raw frame transmission. Neither is possible as an unprivileged user. The receiver itself runs as you, so received files are yours without a `chown`.

Privilege is granted by a named polkit action, `org.omarchy.omdrop.discover`, bound to one helper at `/usr/lib/omdrop/omdrop-discoverable`. That path is root-owned and not writable by the user whose session invokes it, which is the point: a rule that whitelists a script inside someone's home directory hands root to anything running as that user. Turning discoverability on from your own seat needs no password; a remote or inactive session must authenticate as an administrator.

The `brcmfmac-awdl-dkms` package installs both the helper and that action, so there is no manual privilege step and nothing here asks you to grant root to a script in your home directory.

Plugins run unsandboxed inside the long-running Omarchy shell process. Read the code before you enable it.

## Why the code says "airdrop"

"omdrop" is this project's name. The protocol is Apple's, and its wire identifiers are not ours to rename: the mDNS service type is `_airdrop._tcp`, the endpoints are `/Discover`, `/Ask`, and `/Upload`, and the receiver's configuration key is `ReceiverComputerName`. Those names stay. Rename them and nothing interoperates.

AirDrop is a trademark of Apple Inc. omdrop is not affiliated with, authorized by, or endorsed by Apple.

## License

MIT. See [LICENSE](LICENSE).
