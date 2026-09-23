# Omdrop

Send and receive files from nearby Apple devices over AirDrop on Apple Silicon devices running [Omarchy Linux](https://omarchy.org). Works in Everyone and Contacts Only mode, in both directions.

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

`omdrop setup` puts the `omdrop` command on your PATH, at `~/.local/bin/omdrop`. It runs on first use from the panel; run it by hand after installing if you want the command straight away. A file already there that is not ours is left alone.

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

Click the Omdrop icon (a triangle dropping into a box) to open the panel. Inside:

- **The switch** turns receiving on and off. Right-clicking the bar icon does the same without opening the panel.
- **Stay visible for** sets how long receiving mode lasts, from one file to always on.
- **They see you as** sets the name of your device as others see it. Defaults to this machine's short hostname.
- **Save files to** shows the download folder, which defaults to `~/Downloads`. Click it to choose another folder in Files (Nautilus). Where Nautilus isn't installed, the desktop's own file chooser opens instead.

A file that arrives is saved there, copied to the clipboard, and announced in a notification. Click the notification to open it in whatever application handles that file type. Nothing opens on its own.

Everything the panel does is also available from the command line:

```bash
omdrop on 15         # visible to everyone for 15 minutes
omdrop on -c 10m     # Contacts Only may send, for 10 minutes
omdrop on -e 10m     # Everyone nearby may send (the default)
omdrop on once       # until one file arrives
omdrop status        # current state of omdrop
omdrop name "Study Mac"
omdrop dir ~/Drops
omdrop limit 30      # cap one transfer at 30% of currently free disk space
```

`start` and `stop` work in place of `on` and `off`, `list` in place of `peers`, and `vis` or `viz` in place of `visibility`.

#### Contacts Only

By default anyone nearby can send to you. To accept files only from people you
choose:

```bash
omdrop senders add you@icloud.com       # an Apple ID email or phone number
omdrop senders list
omdrop visibility contacts              # only those senders are accepted
omdrop visibility everyone              # back to the default
```

A sender is accepted only if Apple's signature over their identity record is
valid, that record is bound to the certificate on the live connection, and one
of its identifiers is on your list. Anyone else is refused before a single byte
of the file is read.

A sender who identifies itself and is not on your list is not answered at
discovery either, so it does not see this machine in its share sheet at all.

**That hides you from an honest stranger, and only from an honest one.** A
sender presents its Apple-issued certificate when it offers a file and never
when it discovers, so at discovery there is nothing binding the identity record
to the connection: someone replaying a known contact's record would still be
answered — and would then be refused when they tried to send. Presence is
hidden on a best-effort basis; *receiving* is enforced properly.

A device that sends no identity record, or one that cannot be verified, is
still answered. "We could not tell who this is" must not quietly become "hide
from a device nobody has tested this against", and the transfer gate refuses
such a sender anyway.

Measured 2026-09-20, before the discovery gate: a Mac on a different Apple ID
received a full `/Discover` response and was then refused at `/Ask` with 403 in
23 ms, before any file data was read. A Mac on a known account was accepted and
the file stored.

The list holds addresses in the clear so you can read and edit it. They are
hashed at comparison time and never written to a log.

An empty list refuses everyone, and `omdrop visibility contacts` says so when
that is the case.

The receiver defaults to a maximum transfer size of 30% of the free space on
the download disk. Run `omdrop limit PERCENT` to set a value from 1 to 90. The
receiver also preserves at least 1 GiB of free space, even when the configured
percentage would allow a larger transfer.

### Sending

Open the panel and expand **Send to peers** (or press `n`). If Omdrop is off, expanding it turns Omdrop on for the time the slider shows, because a receive window is what hears the devices around you.

- **The radar** draws each nearby device as a dot. A brighter dot, closer to the centre, means a stronger signal. It does not show a direction or a distance.
- **Names** appear beside the dots as devices answer, and each named device joins the list right under the heading with a brief flash. Until then, the line under the heading reads "Searching for peers...", then "Resolving peer names...". While the section is open, the panel asks for names continuously. That means connecting to each device and briefly announcing this Apple ID over Bluetooth (see `peers -n` below), and it stops when you close the section.
- **Sending**: click a name in the list, or a labelled dot, and choose a file. The chooser opens in the folder you last sent from, or in the download folder the first time and whenever that folder is gone. The row shows the send's progress.
- **Sound**: each named device pings as the sweep passes its dot, higher and louder for a stronger signal, and blips when its name first appears. Unnamed dots stay silent. The speaker in the radar's bottom-right corner mutes it, and the setting is remembered (`omdrop sound on|off`).

The command line does the same. Turn a window on first:

```bash
omdrop on                                       # turn on omdrop, so devices are seen
omdrop peers -n                                 # lists nearby devices, with names (-n)
omdrop send ~/photo.jpg                         # send to the only nearby device
omdrop send ~/photo.jpg MyMac                   # send to the device named "MyMac"
omdrop send --to 6c:58:23 ~/photo.jpg           # --to specify any part of an address, or a name
omdrop send --wait 120 ~/photo.jpg iPhone       # keep trying to send to iPhone for two minutes
omdrop send --verbose ~/photo.jpg               # the protocol log, for a bug report
```

`omdrop peers` reports an address and a signal strength for each device. AWDL addresses are randomized per session, so expect them to change.

`omdrop peers -n` also asks each device for its name. Asking means connecting to it, and briefly announcing this Apple ID over Bluetooth, so it is opt-in. It takes 15 to 20 seconds.

The announcement is what makes naming work at all on a device set to **Contacts Only**: that device keeps its AirDrop service shut until it recognizes a nearby sender, the same way it wakes for an Apple device whose share sheet has just opened. It runs only while the lookup runs, and `omdrop peers -n --no-wake` skips it — names then come only from devices that happen to be listening already.

A device answers only while its AirDrop service is up, so `(no response)` says one thing: nothing answered on the AirDrop port. `(anonymous)` means the device answered but withheld its name, which it does when it does not recognize the sender.

The receiving Apple device can be set to **Everyone** or to **Contacts Only**; both work. The recipient sees a prompt naming this computer and has to accept it, exactly as they would from an Apple device.

Contacts Only needs an Apple-issued sender identity installed, which is what the receiving device checks you against. Without one, set the receiver to **Everyone**, or **Everyone for 10 Minutes** on iOS.

**A Mac** answers immediately, whether or not its Finder AirDrop window is open.

**An iPhone** only listens in short bursts, so a single attempt is a coin flip. `omdrop send` polls for the moment its receiver comes up and sends then, for 30 seconds by default. `--wait SECONDS` extends that; `--wait 0` gives up as soon as the phone refuses a connection. Opening a share sheet on the phone, or receiving anything on it, brings its receiver up.


## Privileges

The radio helper runs as root through `pkexec`, because configuring AWDL means vendor command passthrough on the wireless interface and raw frame transmission. Neither is possible as an unprivileged user. The receiver itself runs as you, so received files are yours without a `chown`.

Privilege is granted by a named polkit action, `org.omarchy.omdrop.discover`, bound to one helper at `/usr/lib/omdrop/omdrop-discoverable`. That path is root-owned and not writable by the user whose session invokes it, which is the point: a rule that whitelists a script inside someone's home directory hands root to anything running as that user. Turning discoverability on from your own seat needs no password; a remote or inactive session must authenticate as an administrator.

The `brcmfmac-awdl-dkms` package installs both the helper and that action, so there is no manual privilege step and nothing here asks you to grant root to a script in your home directory.

The UFW exception belongs to the receiver, not the driver package. Omdrop runs `sudo ufw` with fixed arguments in the visible dependency-install terminal, after the package work. `sudo` normally reuses the authentication from installing the driver, so this does not cause a second password prompt. The rule is safe to apply repeatedly; UFW skips an identical existing rule.

## Problems and Contributions
If you encounter a bug, or have a feature request, [create an Issue](https://github.com/brentkearney/omdrop-plugin/issues) here. Or better yet, have your agent fix or implement it, and [create a Pull Request](https://github.com/brentkearney/omdrop-plugin/pulls). I'm happy to review and merge.

#### Known Bugs / Limitations
- One file per `omdrop send`. To send several, run it once per file.
- A refused Contacts Only transfer shows as "Waiting..." on iOS rather than an error, so a refusal can look like a hang on the sending device.

## Trademark
"AirDrop" is a trademark of Apple Inc. Omdrop is an independent project that is not affiliated with, authorized by, or endorsed by Apple.

## License

MIT. See [LICENSE](LICENSE).
