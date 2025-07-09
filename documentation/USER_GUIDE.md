# MEP System User Guide

## Table of Contents
- [Data Collection](#data-collection)
  - [Connecting to the MEP](#connecting-to-the-mep)
  - [Setting the Jetson time](#setting-the-jetson-time)
  - [Start the data collection script](#start-the-data-collection-script)
- [System Update](#system-update)
  - [Zynq Update](#zynq-update)
  - [Jetson Update](#jetson-update)
    - [Update Docker](#update-docker)
    - [Update Ansible](#update-ansible)
    - [Update mep-examples](#update-mep-examples)
- [User Laptop Configuration](#user-laptop-configuration)

## Data Collection

### Connecting to the MEP

The data collection scripts on the MEP are run by connecting to the Jetson 
in the MEP over a network connection. If you are connecting on a home or school
network, a dhcp server on that network will supply an IP address that you can 
use to log on to the MEP. If you are running with your laptop directly connected to 
the MEP, the MEP will wait for 20 seconds after boot and will then run a DHCP server 
of it's own. The address of the MEP on this network is 192.168.33.1. You can check
that you are connected to the local network by checking your laptop's IP address, 
which should be 192.168.33.X. On Ubuntu this can be done by running:

   ```bash
   ip addr
   ```

### Setting the Jetson time
The software on the MEP supports GPS for positiong telemetry, but the
GPS time is not currently synchronized to the Jetson clock. Since the 
Jetson acts as a time server for the RFSoC, the time needs to be accurate
before the capture scripts can run. Until the GPS time synchronization is
available, there is a workaround script in this repository that can push
the time on your laptop to the Jetson. 

The first step is to make sure you can run ssh commands on the Jetson without
waiting for entering a password (which will delay the time sync.) You will need
to copy your ssh key to the Jetson with:

   ```bash
   ssh-copy-id mep@192.168.33.1
   ```
If your key doesn't exist, you will first need to create it with:

   ```bash
   ssh-keygen -t rsa
   ```
This will only need to be run once per laptop/Jetson pair. Once the key has been
copied, you will need the time synchronization script on your laptop. To get it clone
the mep-examples repository locally:

   ```bash
   cd <the directory you want the script in>
   git clone https://github.com/spectrumx/mep-examples.git
   ```
Like the ssh key step, this only needs to be run once. Now that the script is available you
can update the time on the Jetson with:

   ```bash
   cd mep-examples/scripts
   system_time_sync.bash 192.168.33.1
   ```
This will report the resulting difference in time between your laptop and the Jetson. It should be
between 10s to 100s of milliseconds. Unlike the first few steps, this will need to run every time
the Jetson is power-cycled.

### Start the data collection script

You can now connect to the MEP over either RDP or ssh. To connect over ssh run:

   ```bash
   ssh -X mep@192.168.33.1
   ```
This will result in a terminal prompt on the MEP. At this point you can run the data collection script. 
First go to the Jetson copy of the mep-example repo:

   ```bash
   cd /opt/mep-examples/
   ```
And go to the experiments directory:

   ```bash
   cd experiments
   ```
In the experiments directory there are two scripts, one for each tuner, ```run_sweep_VLA_LMX.bash``` and
```run_sweep_VLA_VALON.bash``. Check which tuner your MEP is using and run that script. For example, to
run the script for a MEP with the TI LMX2820 tuner, run: 

   ```bash
   ./run_sweep_VAL_LMX.bash
   ```
This script will kick of the applications on the RFSoC and Jetson that are necessary for data capture
and will start a sweep from 7GHz to 8.5GHz. The capture paramters are hard-coded in the script. If
you need to change them, make a copy of the script and modify it. The script is then also a record of
how the applications were executed. 

   ```bash
   cp run_sweep_VLA_LMX.bash run_sweep_VLA_LMX_250708_siteA.bash
   nano run_sweep_VLA_LMX_250708_siteA.bash
   <make and save edits>
   ./run_sweep_VLA_LMX_250708_siteA.bash
   ```

## System Update

### Zynq Update
Update Zynq to default to 192.168.20.100 when DHCP not available

1. Connect to MEP (must have internet access)
2. Connect to zynq 
   ```bash
   ssh xilinx@192.168.20.100
   ```
3. Update git repo
   ```bash
   cd /opt/git/rfsoc_qsfp_10g
   git pull
   ```
4. Run update script
   ```bash
   cd boards/RFSoC4x2/rfsoc_qsfp_offload/scripts/
   ./update_network.bash
   ```

### Jetson Update 

### Update Docker
```bash
docker compose pull && docker compose up -d --force-recreate
docker compose build recorder
docker compose down
docker compose up -d
```

### Update Ansible
```bash
cd /opt/ansible
sudo git pull
sudo python3 run.py
```

### Update mep-examples (ansible may have done this already)
```bash
cd /opt/mep-examples
git pull
```

## User Laptop Configuration
Clone a copy of the mep-examples repo so the system_time_sync.bash script is available

Open a terminal (WSL, xterm, etc)
```bash
git clone https://github.com/spectrumx/mep-examples.git
```

If on Ubuntu, download the Remmina Remote Desktop (RDP) client

```bash
sudo apt update remmina
```