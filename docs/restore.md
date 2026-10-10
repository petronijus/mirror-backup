# Restoring from a restic job

What a restic job keeps lets you restore one file, browse any snapshot, or
rebuild a whole system on an empty disk. This page covers all three.

Commands for a **system job** run as root through the root-owned command.
Root must never run the user's `~/.local/bin` copy.

```bash
M=/usr/local/lib/mirror-backup/mirror-backup
sudo $M system restic <job> -- <restic arguments>
```

`system restic` sets the repository (`<destination>/repo`), its password file
and the cache, then runs `restic`. For a **user job**, run `restic` yourself
with `-r <destination>/repo --password-file <its key>`.

## Which snapshots there are

```bash
sudo $M system restic backup-system -- snapshots
sudo $M system restic backup-system -- snapshots --host omarchy --latest 3
```

A copy job's repository (the NAS) has the same snapshots under other IDs; use
`backup-system-nas` to look there.

## One file or folder

```bash
# what a snapshot holds
sudo $M system restic backup-system -- ls latest /home/<user>/.config/hypr
# restore into a scratch directory, then copy back what you need
sudo $M system restic backup-system -- restore latest --target /tmp/restore \
    --include /home/<user>/.config/hypr
```

`latest` is the newest snapshot; give a snapshot ID (`snapshots` lists them)
to go back further. Restoring with `--target /` overwrites files in place. Use
it only when you mean to.

## Browsing every snapshot

```bash
sudo mkdir -p /mnt/restic
sudo $M system restic backup-system -- mount /mnt/restic    # stays in the foreground
# another terminal: /mnt/restic/snapshots/<time>/…, /mnt/restic/hosts/omarchy/latest/…
```

Ctrl+C unmounts.

## What every snapshot also carries

When a system job has the pre-command `mirror-backup-system-meta`, each
snapshot holds `/var/lib/mirror-backup/meta/`, written just before the
snapshot was taken:

| File | What it says |
|------|--------------|
| `partitions-<disk>.sfdisk` | the partition table, restorable with `sfdisk` |
| `lsblk.txt`, `blkid.txt`, `findmnt.txt`, `fstab`, `crypttab` | disks, UUIDs, mounts |
| `btrfs-subvolumes.txt` | the subvolume layout of a btrfs root |
| `luks-<dev>.dump`, `luks-<dev>.img` | the LUKS header, and a header backup |
| `efibootmgr.txt` | the firmware boot entries |
| `packages-*.txt`, `flatpak.txt`, `snaps.txt` | what was installed |
| `os-release`, `uname`, `taken-at` | which system this was, and when |

Print one without restoring anything:

```bash
restic -r <repo> dump latest /var/lib/mirror-backup/meta/btrfs-subvolumes.txt
```

## The whole system, on bare metal

This example is an Omarchy install: Arch with btrfs on LUKS, booted by
Limine. It was walked through end to end on 2026-10-10: the system restored
into an empty VM disk booted to its login screen. The steps carry over to other layouts; the snapshot's `meta/`
says what the original looked like. You need:

* a USB stick with the Arch (or Omarchy) ISO, booted in UEFI mode;
* the repository: the BACKUP disk, or the NAS copy over the network;
* the repository password (in a password manager, never on the backed-up disk).

### 1. In the live system: restic and the repository

```bash
systemctl start pacman-init     # returns once the live system's keyring is ready
pacman -Sy restic
# the BACKUP disk …
mkdir -p /run/backup && mount /dev/disk/by-label/BACKUP /run/backup
REPO=/run/backup/mirror-backup/omarchy-system/repo
# … or the NAS copy
mkdir -p /run/nas && mount -t nfs -o ro <nas>:/<export> /run/nas
REPO=/run/nas/BACKUP/mirror-backup/omarchy-system/repo

read -rs PW && printf '%s\n' "$PW" > /root/restic.key && unset PW
export RESTIC_REPOSITORY=$REPO RESTIC_PASSWORD_FILE=/root/restic.key
restic snapshots --host omarchy
```

The live system runs from a small RAM disk: give restic its cache on the
target once it is mounted (step 5), or run it with `--no-cache`.

### 2. What the old system looked like

```bash
for f in partitions-nvme1n1.sfdisk lsblk.txt fstab btrfs-subvolumes.txt efibootmgr.txt; do
    restic dump latest /var/lib/mirror-backup/meta/$f > /root/$f
done
```

(`lsblk.txt` names the disk the system was on.)

### 3. Partitions, encryption, file systems

`DISK` is the empty target disk. The partition table can be replayed when the
new disk is at least as large. Otherwise make an ESP (2 GiB) and one Linux
partition by hand.

```bash
DISK=/dev/nvme0n1
sed '/^label-id:/d; s/, uuid=[^,]*//' /root/partitions-nvme1n1.sfdisk | sfdisk "$DISK"   # new IDs
mkfs.vfat -F 32 "${DISK}p1"
cryptsetup luksFormat --type luks2 "${DISK}p2"
cryptsetup open "${DISK}p2" root
mkfs.btrfs /dev/mapper/root
```

The LUKS header backup in `meta/` restores the **original** disk's header, for
when the disk survived and its header did not (`cryptsetup luksHeaderRestore`).
A new disk gets a new LUKS volume, as above.

### 4. Subvolumes and mounts

Create the subvolumes `btrfs-subvolumes.txt` lists. For Omarchy that is `@`, `@home`,
`@log` and `@pkg`, plus `swap` and `.snapshots` inside `@`.

```bash
mount /dev/mapper/root /mnt
for s in @ @home @log @pkg; do btrfs subvolume create /mnt/$s; done
umount /mnt
o=compress=zstd:3
mount -o $o,subvol=@ /dev/mapper/root /mnt
mkdir -p /mnt/{home,var/log,var/cache/pacman/pkg,boot}
mount -o $o,subvol=@home /dev/mapper/root /mnt/home
mount -o $o,subvol=@log  /dev/mapper/root /mnt/var/log
mount -o $o,subvol=@pkg  /dev/mapper/root /mnt/var/cache/pacman/pkg
mount "${DISK}p1" /mnt/boot
btrfs subvolume create /mnt/.snapshots
btrfs subvolume create /mnt/swap
```

### 5. The files

```bash
export RESTIC_CACHE_DIR=/mnt/.restore-cache
restic restore latest --host omarchy --target /mnt
rm -rf /mnt/.restore-cache
```

The snapshot covers `/`, `/boot`, `/home`, `/var/log` and the extra paths
the job lists. If some of those were bind mounts from a disk that is still
there (a dual boot whose `~/Documents` lives on the other system's disk, say),
leave them out with `--exclude /home/<user>/Documents …`. The restored fstab
mounts them again.

### 6. Make it boot

The new partitions have new UUIDs; the restored configuration names the old ones.

```bash
blkid    # the new UUIDs and PARTUUIDs
OLD_BTRFS=… OLD_ESP=… OLD_LUKS=…   # from meta/blkid.txt, or /mnt/etc/fstab and /mnt/etc/default/limine
sed -i "s/$OLD_BTRFS/<new btrfs UUID>/g; s/$OLD_ESP/<new ESP UUID>/g" /mnt/etc/fstab
sed -i "s/$OLD_LUKS/<new PARTUUID of the LUKS partition>/g" /mnt/etc/default/limine
arch-chroot /mnt
btrfs filesystem mkswapfile --size 64g /swap/swapfile   # the size fstab had
limine-mkinitcpio                 # initramfs + the unified kernel image, with the new cmdline
limine-update                     # the Limine EFI binary
exit
efibootmgr --create --disk /dev/nvme0n1 --part 1 --label Limine \
    --loader '\EFI\limine\limine_x64.efi'
umount -R /mnt && reboot
```

Omarchy boots a **unified kernel image** (`/boot/EFI/Linux/omarchy_*.efi`) with
the kernel command line baked in. The restored image still names the old
LUKS PARTUUID and will not find the root volume, so it has to be rebuilt.
`mkinitcpio -P` finds no presets here; `limine-mkinitcpio` is the command.
On a system that boots a separate initramfs, `mkinitcpio -P` and the boot
loader's own config are what to update instead.

### 7. After the first boot

What the job excludes comes back from where it lives:

* Flatpaks: `flatpak install $(cut -f1 meta/flatpak.txt)`.
* Container images: pulled again.
* Caches: rebuilt on use.
* Steam libraries: reinstalled.

Then check the backups themselves. `mirror-backup status` should list the
system jobs, and the first scheduled run takes a new snapshot of the restored
system.
