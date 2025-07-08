import logging
from src.mep_tuner import MEPTuner
from src.tuner_valon import ValonSynth

#if you need to calibrate the power output, make a set power function, using the 
# set power function in tuner_valon

class MEPTunerValon(MEPTuner):
    def __init__(self, f_if_mhz):
        super().__init__(f_if_mhz)
        self._f_lo_mhz = float('nan')
        self._device_type = "VALON"
        logging.info(f"itializing {self._device_type} tuner")

        try:
            self.valon = ValonSynth()  #assuming udev
        except Exception as e:
            logging.error(f"ailed to connect to Valon synthesizer: {e}")
            raise

        logging.info(f"initialized {self._device_type} tuner")

    def __del__(self):
        self.valon.close()
        logging.debug("Leaving MEPTunerValon")

    def set_freq(self, f_c_mhz):
       #add more if needed idk
        if f_c_mhz < 0:
            raise ValueError("frequency cannot be negative")
         
        self._f_lo_mhz = f_c_mhz + self._f_if_mhz
        logging.info(f"setting center frequency to {f_c_mhz} MHz")
        logging.info(f"setting local oscillator frequency to {self._f_lo_mhz} MHz")

        result = self.valon.set_freq(self._f_lo_mhz)
        logging.debug(result)

    #possible to implement but not now
    def get_status(self):
        pass
       
       
       
        
