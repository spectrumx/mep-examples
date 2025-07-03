import logging
from src.mep_tuner import MEPTuner

class MEPTunerTest(MEPTuner):
    """Test implementation of MEPTuner interface for unit testing"""
    
    def __init__(self, f_if_mhz):
        """Initialize test implementation with debug logging"""
        super().__init__(f_if_mhz)
        logging.debug("Initializing MEPTunerTest instance")
        self._device_type = "test"
        self._f_lo_mhz = float('nan')

    def __del__(self):
        """Cleanup test implementation resources"""
        logging.debug("Leaving MEPTunerTest")
        super().__del__()

    def set_freq(self, f_c_mhz: float):
        """
        Test implementation of frequency setting
        
        Args:
            frequency (float): Target frequency in MHz for testing
        """
        # Bounds check
        if f_c_mhz < 0:
            raise ValueError("Frequency cannot be negative")

        # Calculate local oscillator frequency
        self._f_lo_mhz = f_c_mhz + self._f_if_mhz

        logging.info(f"Test: Setting center frequency to {f_c_mhz} MHz")
        logging.debug(f"Test: Setting local oscillator frequency to {self._f_lo_mhz} MHz")

        self._state = "active"

    def get_status(self) -> dict:
        """
        Test implementation of status reporting
        
        Returns:
            dict: Test status information including:
                - device_type: "Test"
                - frequency_MHz: Last set frequency
                - state: Current test state
        """
        logging.debug(f"MEP Tuner: {self._device_type} State: {self._state} Frequency: {self._frequency_hz}")
        return {
            "device_type": self._device_type,
            "f_c_mhz": self._f_lo_mhz + self._f_if_mhz,
            "f_lo_mhz": self._f_lo_mhz,
            "state": self._state
        }