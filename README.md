# Omdrop

Receive files from nearby Apple devices, over AirDrop, on Apple Silicon devices running [Omarchy Linux](https://omarchy.org). Sending is not working yet, but will be soon. Also requires the radio to be on the 2.4GHz frequency, so if it is on 5GHz, we automatically switch to 2.4GHz when receiving mode is enabled, and back when it is disabled. We hope to overcome this limitation soon as well.

https://github.com/user-attachments/assets/3e415609-6e25-48b0-b55f-58252c65356b

Depends on a [patched wifi driver](https://github.com/brentkearney/omdrop-awdl), which the plugin offers to install on first use.

Provides an Omarchy toolbar menu that:
- Turns receiving mode on/off
- Sets the name of your device, as it appears to 
- Sets the file download location

Notification pops up when a file is received. JPEG or txt files get automatically copied to clipboard. Clicking the notification popup opens the file with the default app for the file type.

## Requirements

1. **Broadcom Wi-Fi whose firmware implements AWDL** — the link layer Apple Silicon devices use to talk to each other directly. Verified on the BCM4387 (`14e4:4433`) in the MacBook Pro 16-inch, M1 Pro. Other Apple Broadcom parts are plausible and untested. Intel, MediaTek, and Qualcomm cards cannot do this.
2. **A patched `brcmfmac` kernel module** provided as a DKMS package, so it survives kernel upgrades.


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

That leaves the driver package alone. Remove the patched wifi driver separately with `pacman -R brcmfmac-awdl-dkms`.

## Use

Click the parachute to open the panel. Inside:

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
```


## Privileges

The radio helper runs as root through `pkexec`, because configuring AWDL means vendor command passthrough on the wireless interface and raw frame transmission. Neither is possible as an unprivileged user. The receiver itself runs as you, so received files are yours without a `chown`.

Privilege is granted by a named polkit action, `org.omarchy.omdrop.discover`, bound to one helper at `/usr/lib/omdrop/omdrop-discoverable`. That path is root-owned and not writable by the user whose session invokes it, which is the point: a rule that whitelists a script inside someone's home directory hands root to anything running as that user. Turning discoverability on from your own seat needs no password; a remote or inactive session must authenticate as an administrator.

The `brcmfmac-awdl-dkms` package installs both the helper and that action, so there is no manual privilege step and nothing here asks you to grant root to a script in your home directory.

## Trademark
"AirDrop" is a trademark of Apple Inc. Omdrop is an independent project that is not affiliated with, authorized by, or endorsed by Apple.

## License

MIT. See [LICENSE](LICENSE).
