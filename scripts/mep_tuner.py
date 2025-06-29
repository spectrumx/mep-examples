"""
mep_tuner.py

Abstract base class for MEP tuner devices defining the standard tuner interface. 

Author: nicholas.rainville@colorado.edu
"""

from abc import ABC, abstractmethod

class MEPTuner(ABC):
    def __init__(self):
        """Constructor - can be overridden by child classes"""
        pass

    def __del__(self):
        """Destructor - can be overridden by child classes"""
        pass

    @abstractmethod
    def set_freq(self, frequency):
        """Abstract method to set frequency - must be implemented by child classes"""
        pass

    @abstractmethod
    def get_status(self):
        """Abstract method to get status - must be implemented by child classes"""
        pass