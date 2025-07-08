# MEP System User Guide

## Table of Contents
- [System Update](#system-update)
  - [Zynq Update](#zynq-update)
  - [Jetson Update](#jetson-update)
    - [Update Docker](#update-docker)
    - [Update Ansible](#update-ansible)
    - [Update mep-examples](#update-mep-examples)
- [User Laptop Configuration](#user-laptop-configuration)

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
