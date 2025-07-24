import zmq
import time
import logging
import ast

RFSOC_IP = "192.168.20.100"

GREEN = "\033[92m"
BLUE = "\033[94m"
RED = "\033[91m"
RESET = "\033[0m"

class MEPRFSoC:
    def __init__(self, pub_port=60200, sub_port=60201, sub_ip=RFSOC_IP):
        pub_socket_str = f"tcp://*:{pub_port}"
        sub_socket_str = f"tcp://{sub_ip}:{sub_port}"

        self.context = zmq.Context()
        self._zmq_pub = self.context.socket(zmq.PUB)
        self._zmq_pub.bind(pub_socket_str)

        self._zmq_sub = self.context.socket(zmq.SUB)
        self._zmq_sub.connect(sub_socket_str)
        self._zmq_sub.setsockopt_string(zmq.SUBSCRIBE, "")
        self._zmq_poller = zmq.Poller()
        self._zmq_poller.register(self._zmq_sub, zmq.POLLIN)

        logging.info(f"Connecting to RFSoC at {sub_ip}")
        time.sleep(0.2)  # Allow ZMQ to settle

    def _send_command(self, command_str):
        self._zmq_pub.send_string(command_str)
        logging.debug(f"Sent: {BLUE}{command_str}{RESET}")

    def reset(self):
        self._send_command("cmd reset")

    def capture_next_pps(self):
        self._send_command("cmd capture_next_pps")

    def set_freq_metadata(self, f_c_hz):
        """
        Set the RF center frequency metadata (used for recorder tagging).
        """
        self._send_command(f"cmd set freq_metadata {f_c_hz}")

    def set_freq_IF(self, freq_mhz):
        """
        Set the ADC IF frequency (used to configure NCO/mixer) in MHz.
        """
        self._send_command(f"cmd set freq_IF {freq_mhz}")

    def get_tlm(self, verbose=False):
        timeout_s = 1.5
        start_time = time.time()

        # Clear old messages
        while True:
            try:
                self._zmq_sub.recv_string(zmq.DONTWAIT)
            except zmq.Again:
                break
            if time.time() - start_time > timeout_s:
                logging.error("Timeout while clearing ZMQ queue")
                return None

        # Request telemetry
        self._send_command("cmd get tlm")

        # Wait for telemetry
        socks = dict(self._zmq_poller.poll(timeout_s * 1e3))  # ms
        if self._zmq_sub in socks and socks[self._zmq_sub] == zmq.POLLIN:
            tlm_str = self._zmq_sub.recv_string()
            if verbose:
                logging.debug(f"Telemetry received: {tlm_str}")
        else:
            return None

        # Parse telemetry
        if not tlm_str.startswith("tlm "):
            logging.warning("Non-telemetry message received")
            return None

        try:
            _, payload = tlm_str.split(" ", 1)
            fields = payload.split(";")
            if len(fields) < 6:
                logging.error("Incomplete telemetry message")
                return None
            channels_list = ast.literal_eval(fields[5])
        except Exception as e:
            logging.error(f"Failed to parse telemetry: {e}")
            return None

        return {
            'state': fields[0],
            'f_c_hz': fields[1],
            'f_if_hz': fields[2],
            'f_s': fields[3],
            'pps_count': fields[4],
            'channels': channels_list
        }

    def get_msg(self, verbose=True):
        socks = dict(self._zmq_poller.poll(10))  # Poll every 10ms
        if self._zmq_sub in socks and socks[self._zmq_sub] == zmq.POLLIN:
            msg = self._zmq_sub.recv_string()
            if verbose:
                logging.debug(f"Received message: {BLUE}{msg}{RESET}")
            return msg
        return None

    def __del__(self):
        try:
            self.reset()
        except:
            pass
        self._zmq_pub.close()
        self._zmq_sub.close()

