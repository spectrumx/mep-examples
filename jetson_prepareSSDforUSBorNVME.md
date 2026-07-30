````markdown
# Make a Jetson NVMe Bootable from USB or M.2

Use a filesystem UUID instead of `/dev/sda1` so the same NVMe boots both through a USB enclosure and from the Jetson’s internal M.2 slot.

## 1. Identify the root partition

Connect the NVMe to a Linux computer:

```bash
lsblk -f
```

Find the large `ext4` partition, typically partition 1, such as `/dev/sdi1`.

Confirm it contains the Jetson root filesystem:

```bash
ls /media/$USER/<UUID>
```

You should see directories such as `boot`, `etc`, `home`, `usr`, and `var`.

## 2. Set the device, mount point, and UUID

Replace `/dev/sdi1` with the actual root partition:

```bash
ROOTDEV=/dev/sdi1
ROOTUUID=$(sudo blkid -s UUID -o value "$ROOTDEV")
ROOT=/media/$USER/$ROOTUUID

echo "$ROOTDEV"
echo "$ROOTUUID"
echo "$ROOT"
```

## 3. Check the current configuration

```bash
sudo grep -nE 'root=|/dev/sda1|/dev/nvme' \
  "$ROOT/boot/extlinux/extlinux.conf" \
  "$ROOT/etc/fstab"
```

## 4. Back up and update the boot configuration

```bash
sudo cp -a \
  "$ROOT/boot/extlinux/extlinux.conf" \
  "$ROOT/boot/extlinux/extlinux.conf.before-portable-root"
```

Replace `root=/dev/sda1` with the filesystem UUID:

```bash
sudo sed -i \
  "s#root=/dev/sda1#root=UUID=$ROOTUUID#" \
  "$ROOT/boot/extlinux/extlinux.conf"
```

If `/etc/fstab` also references `/dev/sda1` for `/`, update it:

```bash
sudo sed -i \
  "s#^/dev/sda1[[:space:]]\\+/[[:space:]]#UUID=$ROOTUUID / #" \
  "$ROOT/etc/fstab"
```

## 5. Verify and unmount

```bash
sudo grep -nE 'root=|UUID=|/dev/sda1' \
  "$ROOT/boot/extlinux/extlinux.conf" \
  "$ROOT/etc/fstab"
```

The boot line should now contain:

```text
root=UUID=<filesystem-UUID>
```

Unmount safely:

```bash
sync
sudo umount "$ROOT"
```

The same NVMe can now boot as `/dev/sda1` through USB or `/dev/nvme0n1p1` in the internal M.2 slot.
````
