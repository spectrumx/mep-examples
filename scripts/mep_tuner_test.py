import logging
from mep_tuner import MEPTuner

class MEPTunerTest(MEPTuner):
    """Test implementation of MEPTuner interface for unit testing"""
    
    def __init__(self):
        """Initialize test implementation with debug logging"""
        super().__init__()
        logging.debug("Initializing MEPTunerTest instance")
        self._test_state = "initialized"  # Test-specific state tracking
        self._device_type = "test"
        self._frequency_hz = float('nan')

    def __del__(self):
        """Cleanup test implementation resources"""
        logging.debug("Cleaning up MEPTunerTest instance")
        self._test_state = "deinitialized"
        super().__del__()

    def set_freq(self, frequency_mhz: float):
        """
        Test implementation of frequency setting
        
        Args:
            frequency (float): Target frequency in MHz for testing
        """
        logging.debug(f"Test: Setting frequency to {frequency_mhz} MHz")
        
        if frequency_mhz < 0:
            raise ValueError("Frequency cannot be negative")

        self._frequency_hz = frequency_mhz * 1e6
        self._test_state = "active"

    def get_status(self) -> dict:
        """
        Test implementation of status reporting
        
        Returns:
            dict: Test status information including:
                - device_type: "Test"
                - frequency_MHz: Last set frequency
                - state: Current test state
        """
        logging.debug(f"MEP Tuner: {self._device_type} State: {self._test_state} Frequency: {self._frequency_hz}")
        return {
            "device_type": self._device_type,
            "frequency_mHz": self._frequency_hz,
            "state": self._test_state
        }