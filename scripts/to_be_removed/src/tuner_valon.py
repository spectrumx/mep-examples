#!/usr/bin/env python3
"""
Valon 5015/5019 RF Synthesizer CLI Tool

This tool configures the synthesizer via USB serial connection.
NOTE: Internal error from serial is thrown someyimes when the VALON is run for too long (hours).
possible solutions include:
    - a reset function (although this will introduce time delay in a sweep)
    - higher/lower baud rate
    - shorter sleep after send
At a power input of 0, the relative output is equal to that of the LMX2820.
Alisa Yurevich (Alisa.Yurevich@tufts.edu) 06/2025 
"""
import argparse
import serial  
import time

class ValonSynth():
    """
    Class for communicating with a Valon 5015 RF Synthesizer
    over a serial connection.
    """

    def __init__(self, port = "/dev/valon5015", baudrate = 9600): # baud rate can be made higher accordingly
        """
        Initialize the serial connection to the Valon device.
        Assumes a persistent udev symlink has been created (e.g., /dev/valon5015).
        """
        self.port = port
        self.ser = serial.Serial(port, baudrate=baudrate, timeout=1)

        if not self.ser.is_open: 
            self.ser.open()

        # one way to clear -> can also turn dtr on and off
        self.ser.reset_input_buffer() 

    def send(self, command):
        """
        Send a command string to the Valon over serial.
        Appends carriage return. Returns any response.
        """
        self.ser.write((command + "\r").encode())
        time.sleep(0.1)
        response = b""

        while self.ser.in_waiting:
            response += self.ser.read(self.ser.in_waiting)

        return response.decode(errors='ignore')

    def set_freq(self, freq_mhz):
        """
        Set the output frequency of the synthesizer.
        """
        cmd = f"F{freq_mhz}MHz"
        print(f"Sending frequency command: {cmd}")
        return self.send(cmd)
    
    def set_power(self, mod_dB):
        """
        Set output power level. Valid Range -50 - 20. Can be brought lower 
        configuring extra settings in the Valon.
        """
        cmd = f"PWR {mod_dB}"
        print(f"Sending power command: {cmd}")
        return self.send(cmd)

    def close(self):
        """
        Close serial connection.
        """
        self.ser.reset_input_buffer()
        self.ser.close()

 