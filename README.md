# Mobile Experiment Platform (MEP) Examples

## Overview
The MEP is a platform for RF spectrum monitoring, propagation expermients, and coexistance experiments. The core components of the MEP are the Analog Front End (AFE), the RFSoC 4x2 Software Defined Radio (SDR), and the Jetson Orin NX Edge AI computer. These components provide the capability to both receive and transmit wideband RF data, as well as to process and store data for later analysis.

### Analog Front End (AFE)
The signal chain begins and ends at the AFE board. This board includes the analog compenents necessary to filter and amplify RF signals and is connected direclty to the RF inputs and outputs of the SDR. It also includes a GPS Disciplined Oscillator (GPSDO) and an embedded microcontroller which monitors the GPS and front end components and which can be used to switch between different RF inputs. The software for this microcontroller can be found in the repository at: 

https://github.com/spectrumx/mep-afe-rp2040.git


### RFSoC 4x2 
The RFSoC 4x2 is a development board built around the ZYNQ Ultrascale+ RFSoC ZU48DR FPGA. This FPGA includes a quad-core Cortex-A53 ARM processor, FPGA programmable logic, 8x 14-bit 5Gsps ADCs, and 8x 9.85Gsps 14-bit DACs. The RFSoC 4x2 board breaks out 4 of the ADCs and 2 of the DACs, which are then connected to the AFE board. 

The ARM processor on the RFSoC 4x2 runs the Petalinux embedded Linux operating system as well as PYNQ software for interfacing with the FPGA logic. PYNQ includes a Python environment and a Jupyter notebook server. Example python code and python notebooks can be found in this repository. 

### Jetson Orin NX

The Jetson Orin NX is an embedded Edge AI computer which includes a hexa-core Cortex-A78AE ARM processor and a 1024 core NVIDIA Ampere GPU. This processor runs Jetpack Linux, which is based on Ubuntu LTS. The Jetson is connected to the RFSoC over two network interfaces. The first is a gigabit ethernet link which is connected to the ARM processor on the Zynq and which is used for configuration and management of the RFSoC. The second is a 100G ethernet link which is connected directly to the FPGA fabric on the RFSoC 4x2 and which can be used to send and receive high speed data.  

Example software to connect to the RFSoC 4x2 and to send and receive data can be found in this repository.  

## Organization
### RFSoC 4x2 Notebooks
Python notebooks for the RFSoC 4x2 can be found:

notebooks/

### GNU Radio Flow Graphs
GNU Radio flow graphs can be found in: 

gnuradio/

