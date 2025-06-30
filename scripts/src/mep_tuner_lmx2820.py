import logging
from src.mep_tuner import MEPTuner
import src.tuner_lmx2820 as tuner_lmx2820
import board
import busio
from digitalio import DigitalInOut

class MEPTunerLMX2820(MEPTuner):
    """Child class implementation for LMX2820 tuner device"""
    
    def __init__(self, f_if_mhz):
        """Initialize LMX2820 specific hardware/resources"""
        super().__init__(f_if_mhz)
        # Add hardware-specific initialization here
        # Connect to tuner USB/SPI
        ref_freq = 10e6
        ref_doubler = 0
        ref_multiplier = 1
        pre_r_div = 1
        post_r_div = 1

        spi = busio.SPI(
            board.SCK,  # clock
            board.MOSI,  # mosi
            board.MISO,
        )  # miso

        CSpin = DigitalInOut(board.D4)
        CSpin.switch_to_output(value=True)

        # Initialize LMX2820 Register map
        tuner = tuner_lmx2820.LMX2820(ref_freq, ref_doubler, ref_multiplier, pre_r_div, post_r_div)
        tuner_lmx2820.LMX2820StartUp(tuner, spi, CSpin)

        f_c_lo = f_c - self._f_if_mhz
        f_c_lo_ghz = f_c_lo / 1e9
        tuner_lmx2820.LMX2820ChangeFreq(spi, CSpin, tuner, int(f_c_lo_ghz))

    def __del__(self):
        """Clean up LMX2820 specific resources"""
        # Add hardware-specific cleanup here
        super().__del__()

    def set_freq(self, frequency: float):
        """
        Set the frequency for LMX2820 tuner
        
        Args:
            frequency (float): Target frequency in MHz
        """
        if frequency < 0:
            raise ValueError("Frequency cannot be negative")
        self._frequency = frequency
        # Add hardware-specific frequency setting code here

    def get_status(self) -> dict:
        """
        Get current device status
        """
        logging.info(f"LMX2820 Frequency: {self._frequency}")