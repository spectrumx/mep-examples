# Jetson Orin NX Bootloader Recovery Guide
## Target: JetPack 6.0 / L4T R36.3.0 on Ubuntu 24.04 (Noble) host

---

## Prerequisites
- x86_64 Ubuntu host
- Jetson Orin NX connected via USB-C
- Jetson in recovery mode (short out leads on 'recovery' 2-pin port, then power on)

Confirm Jetson is visible in recovery mode on host:
```bash
lsusb | grep -i nvidia
# Should show: NVidia Corp. APX
```

---

## 1. Install dependencies

```bash
sudo apt update
sudo apt install -y qemu-user-static binfmt-support nfs-kernel-server sshpass abootimg
```

---

## 2. Increase USB buffer size

```bash
sudo sh -c 'echo 1000 > /sys/module/usbcore/parameters/usbfs_memory_mb'
```

---

## 3. Fix USB network interface naming

Ubuntu 24.04 renames the Jetson's USB network interface away from `usb0`. This rule forces it to stay as `usb0` for NVIDIA devices only:

```bash
echo 'SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="0955", NAME="usb0"' | sudo tee /etc/udev/rules.d/72-nvidia-usb.rules
sudo udevadm control --reload-rules
```

---

## 4. Download and extract the BSP and root filesystem

```bash
cd ~/Downloads

curl -L -o Jetson_Linux_R36.3.0_aarch64.tbz2 \
  https://developer.nvidia.com/downloads/embedded/l4t/r36_release_v3.0/release/Jetson_Linux_R36.3.0_aarch64.tbz2

tar xf Jetson_Linux_R36.3.0_aarch64.tbz2

curl -L -o Tegra_Linux_Sample-Root-Filesystem_R36.3.0_aarch64.tbz2 \
  https://developer.nvidia.com/downloads/embedded/l4t/r36_release_v3.0/release/Tegra_Linux_Sample-Root-Filesystem_R36.3.0_aarch64.tbz2

sudo tar xpf Tegra_Linux_Sample-Root-Filesystem_R36.3.0_aarch64.tbz2 -C Linux_for_Tegra/rootfs/
```

Note: the root filesystem tarball is ~1.7GB. It is needed to build the ramdisk even though the OS itself will not be flashed.

---

## 5. Apply binaries on the host

Remove conflicting device files first to prevent the script from failing silently:

```bash
cd ~/Downloads/Linux_for_Tegra
sudo rm -f rootfs/dev/random rootfs/dev/urandom rootfs/dev/null rootfs/dev/zero
sudo ./apply_binaries.sh
```

The last line of output should say `L4T BSP package installation completed!`

---

## 6. Flash bootloader only using l4t_initrd_flash

Make sure Jetson is in recovery mode and plugged in, then:

```bash
cd ~/Downloads/Linux_for_Tegra
sudo ./tools/kernel_flash/l4t_initrd_flash.sh \
  -p "-c bootloader/generic/cfg/flash_t234_qspi.xml" \
  --showlogs \
  --network usb0 \
  jetson-orin-nano-devkit internal
```

The script will:
1. Build the flash images
2. Boot the Jetson into a minimal initrd environment over USB
3. SSH into it and flash the QSPI bootloader partitions
4. Reboot the Jetson

You will see repeated `Waiting for target to boot-up...` and `Waiting for device to expose ssh...` messages for several minutes — this is normal. Do not interrupt it.

Once SSH connects, it will flash eMMC then QSPI. It may appear stuck at `Starting to flash to qspi` for several minutes — this is also normal. Do not interrupt it.

When complete you will see:
```
Flash is successful
Reboot device
```

---

## Notes
- The board config `jetson-orin-nano-devkit` is correct for both Orin NX and Orin Nano
- The Jetson does **not** need to boot for this to work — recovery mode bypasses the bootloader entirely
- `l4t_initrd_flash.sh` is the official NVIDIA method for Orin NX — `flash.sh` does not work reliably for this board
- The BSP downloads may require an NVIDIA developer account — if curl returns an HTML page instead of the tarball, log in at developer.nvidia.com and download manually
