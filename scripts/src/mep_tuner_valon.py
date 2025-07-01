"""
mep_tuner_valon.py


Author: alisa.yurevich@tufts.edu
"""
import subprocess
import socket
import json
import os
import logging
import time
import signal
from src.mep_tuner import MEPTuner

TELEM_SOCKET = "/tmp/valon_telem.sock"
CLI_SOCKET = "/tmp/valon_cli.sock"

# launches the service as a subprocess, specifically server.py main
class MEPTunerValon(MEPTuner):
    """
    Child class implementation for Valon Tuner Device
    """
    def __init__(self, f_if_mhz):
        super().__init__(f_if_mhz)
        script_dir = os.path.dirname(__file__)
        valon_service_relpath = os.path.join(script_dir, "..", "..", "extern", "valon-jetson-service", "valon_tel", "server.py")
        self.valon_service_script = os.path.abspath(valon_service_relpath)
        self.startup_timeout = 10                                              
        self.proc = None
        self._f_lo_mhz = float('nan')
        self._device_type = "Valon"

        logging.info(f"Initializing {self._device_type} tuner")

        self._start_valon_service()
        # self._wait_for_service()
        logging.info(f"Initialized {self._device_type} tuner")

    def _start_valon_service(self):
        """
        Start the valon service subprocess
        """
        try:
            if not os.path.exists(self.valon_service_script):
                raise FileNotFoundError(f"valon service script not found: {self.valon_service_script}")
            
            logging.info(f"starting Valon service subprocess: {self.valon_service_script}")
            
            self.proc = subprocess.Popen(
                ["python3", self.valon_service_script],
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid  
            )
            
            logging.info(f"valon service started with PID: {self.proc.pid}")
            
        except Exception as e:
            logging.error(f"failed to start Valon service: {e}")
            raise

    def _send_command(self, cmd):
        """
        Send command to the service via the client socket.
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5.0)
                sock.connect(CLI_SOCKET)
                sock.sendall(cmd.encode('utf-8'))
                response = sock.recv(1024).decode('utf-8').strip()
                return response
        except Exception as e:
            logging.error(f"CLI command failed: {e}")
            raise

    def _get_telemetry(self):
        """
        Read from the servia via telem socket.
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5.0)
                sock.connect(TELEM_SOCKET)
                response = sock.recv(1024).decode('utf-8').strip()
                return json.loads(response)
        except Exception as e:
            logging.error(f"Telemetry request failed: {e}")
            raise

    def __del__(self):
        """
        Stop Valon Service Subprocess
        """
        logging.debug("Leaving Valon Tuner")
        self._stop_valon_service()
        super().__del__()

    def set_freq(self, f_c_mhz : float):
        """
        
        """
        if f_c_mhz < 0:
            raise ValueError("Frequency cannot be negative")
        
        self._f_lo_mhz = f_c_mhz - self._f_if_mhz
        logging.info(f"Setting center frequency to {f_c_mhz} MHz")
        logging.debug(f"Setting local oscillator frequency to {self._f_lo_mhz} MHz")
        self._send_command(f"F{self._f_lo_mhz}MHz")
    
    def get_status(self):
        pass

    def _stop_valon_service(self):
        """
        Stop the Valon service subprocess
        """
        if self.proc and self.proc.poll() is None:
            logging.info("stopping Valon service subprocess")
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logging.warning("valon service didn't stop gracefully, forcing kill")
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                    self.proc.wait()
                
                logging.info("valon service stopped")
                
            except Exception as e:
                logging.error(f"error stopping Valon service: {e}")

    def _wait_for_service(self):
        """
        """
        start = time.time()
        while time.time() - start < self.startup_timeout:
            if os.path.exists(CLI_SOCKET) and os.path.exists(TELEM_SOCKET):
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.connect(CLI_SOCKET)
                    return
                except socket.error:
                    pass
            time.sleep(0.25)
        raise TimeoutError("Valon service failed to start in time")

