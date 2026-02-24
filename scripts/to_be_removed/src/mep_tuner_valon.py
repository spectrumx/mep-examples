"""
Class for MEP control of Valon Tuner

This class instiantes a Valon object and allows frequency control.
NOTE: if you need to calibrate the power output (amongst other sweeping/modulation capabilities),
make a set power function, using the corresponding function in tuner_valon

Alisa Yurevich (Alisa.Yurevich@tufts.edu) 06/2025 
"""

import logging
from src.mep_tuner import MEPTuner
from src.tuner_valon import ValonSynth

class MEPTunerValon(MEPTuner):
    def __init__(self, f_if_mhz):
        super().__init__(f_if_mhz)
        self._f_lo_mhz = float('nan')
        self._device_type = "VALON"
        logging.info(f"initializing {self._device_type} tuner")

        try:
            self.valon = ValonSynth()  
        except Exception as e:
            logging.error(f"failed to connect to Valon synthesizer: {e}")
            raise

        logging.info(f"initialized {self._device_type} tuner")

    def __del__(self):
        self.valon.close()
        logging.debug("Leaving MEPTunerValon")

    def set_freq(self, f_c_mhz):
        # TO DO: more safeguards for input frequency
        if f_c_mhz < 0:
            raise ValueError("frequency cannot be negative")
         
        self._f_lo_mhz = f_c_mhz + self._f_if_mhz
        logging.info(f"setting center frequency to {f_c_mhz} MHz")
        logging.info(f"setting local oscillator frequency to {self._f_lo_mhz} MHz")

        result = self.valon.set_freq(self._f_lo_mhz)
        logging.debug(result)
       
    def get_status(self) -> dict:
        """
        Get current device status
        """
        logging.info(f"Dummy Status")
        
