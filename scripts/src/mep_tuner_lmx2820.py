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
        self._f_lo_mhz = float('nan')
        self._device_type = "LMX2820"
        logging.info(f"Initializing {self._device_type} tuner")

        # Connect to tuner USB/SPI
        ref_freq = 10e6
        ref_doubler = 0
        ref_multiplier = 1
        pre_r_div = 1
        post_r_div = 1

        self.spi = busio.SPI(
            board.SCK,  # clock
            board.MOSI,  # mosi
            board.MISO,
        )  # miso

        self.CSpin = DigitalInOut(board.D4)
        self.CSpin.switch_to_output(value=True)

        # Initialize LMX2820 Register map
        self.tuner = tuner_lmx2820.LMX2820(ref_freq, ref_doubler, ref_multiplier, pre_r_div, post_r_div)
        tuner_lmx2820.LMX2820StartUp(self.tuner, self.spi, self.CSpin)
        logging.info(f"Initialized {self._device_type} tuner")

    def __del__(self):
        """Clean up LMX2820 specific resources"""
        logging.debug("Leaving MEPTunerLMX2820")
        super().__del__()

    def set_freq(self, f_c_mhz : float):
        """
        Set the frequency for LMX2820 tuner
        
        Args:
            frequency (float): Target frequency in MHz
        """
        if (f_c_mhz < 1e3 or f_c_mhz > 22.6e3):
            raise ValueError("Frequency out of bounds")

        # Calculate local oscillator frequency
        self._f_lo_mhz = f_c_mhz - self._f_if_mhz
        f_lo_hz = self._f_lo_mhz * 1e6

        logging.info(f"Setting center frequency to {f_c_mhz} MHz")
        logging.debug(f"Setting local oscillator frequency to {self._f_lo_mhz} MHz")
        tuner_lmx2820.LMX2820ChangeFreq(self.spi, self.CSpin, self.tuner, int(f_lo_hz))

    def get_status(self) -> dict:
        """
        Get current device status
        """
        logging.info(f"LMX2820 Frequency: {self._frequency}")