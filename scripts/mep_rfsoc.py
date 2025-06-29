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
        time.sleep(.2)  # Wait for ZMQ to initialize

        # Check for response?

    def _send_command(self, command_str):
        self._zmq_pub.send_string(f"{command_str}")
        logging.debug(f"Sent: {BLUE}{command_str}{RESET}")

    def reset(self):
        self._send_command("cmd reset")

    def capture_next_pps(self):
        self._send_command("cmd capture_next_pps")

    def set_freq_metadata(self, f_c_hz):
        self._send_command(f"cmd set freq_metadata {f_c_hz}")

    def get_tlm(self, verbose=False):
        timeout_s = .05
        start_time = time.time()

        # Clear zmq queue
        elapsed_time = 0
        while (elapsed_time < timeout_s):
            try:
                self._zmq_sub.recv_string(zmq.DONTWAIT)
            except zmq.Again:
                break
            elapsed_time = (time.time() - start_time)
        
        if (elapsed_time > timeout_s):
            logging.error("Timeout while clearing ZMQ queue")
            return None

        # Request telemetry
        self._send_command("cmd get tlm") 

        # Check zmq for new message
        socks = dict(self._zmq_poller.poll(timeout_s * 1e3))  # Poll with timeout
        if self._zmq_sub in socks and socks[self._zmq_sub] == zmq.POLLIN:
            tlm_str= self._zmq_sub.recv_string()
            if verbose:
                logging.debug(f"Telemetry received: {tlm_str}")
        else :
            return None

        # Parse telemetry
        tlm_parts = tlm_str.split(" ",1)
        if (len(tlm_parts) < 2):
            logging.error("Invalid telmetry message")
            return None
        
        if (tlm_parts[0] != "tlm"):
            logging.warning("Non-tlm message in queue")
            return None

        tlm_data = tlm_parts[1]
        tlm_data_fields = tlm_data.split(';')

        if (len(tlm_data_fields) < 5):
            logging.error("Invalid telemetry fields")
        
        try :
            channels_list = ast.literal_eval(tlm_data_fields[5])  # Convert string to list
        except :
            logging.error("Failed to convert channels to list")
            return None

        # Return dict of telemetry values
        tlm_dict = {
            'state': tlm_data_fields[0],
            'f_c_hz': tlm_data_fields[1],
            'f_if_hz': tlm_data_fields[2],
            'f_s': tlm_data_fields[3],
            'pps_count': tlm_data_fields[4],
            'channels': channels_list
        }

        return tlm_dict

    def get_msg(self, verbose=True):
        socks = dict(self._zmq_poller.poll(10))  # Poll every 10ms
        if self._zmq_sub in socks and socks[self._zmq_sub] == zmq.POLLIN:
            message = self._zmq_sub.recv_string()
            if verbose :
                logging.debug(f"Received message: \n{BLUE}{message}{RESET}")
            return message
        else :
            return None

    def __del__(self):
        self.reset()
        self._zmq_pub.close()
        self._zmq_sub.close()