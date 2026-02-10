'''
# Author: John Marino 2026
# Based on /scripts/src/mep_rfsoc.py by Nicholas Rainville
# Based on \opt\git\rfsoc_qsfp_10g\boards\RFSoC4x2\rfsoc_qsfp_offload\scripts\start_capture_rx.py ZMQ->MQTT conversion by Randy Herban 
'''
import paho.mqtt.client as mqtt
import time
import logging
import json
import threading

RFSOC_IP = "192.168.20.100"
MQTT_BROKER = "192.168.20.1"  # Must match RFSoC broker address
MQTT_PORT = 1883

GREEN = "\033[92m"
BLUE = "\033[94m"
RED = "\033[91m"
RESET = "\033[0m"

class MEPRFSoC:
    def __init__(self, broker=MQTT_BROKER, port=MQTT_PORT, command_topic="rfsoc/command", status_topic="rfsoc/status"):
        """
        Initialize MQTT-based MEP RFSoC controller.
        
        Args:
            broker: MQTT broker address
            port: MQTT broker port
            command_topic: Topic to publish commands to
            status_topic: Topic to subscribe for status/telemetry responses
        """
        self.broker = broker
        self.port = port
        self.command_topic = command_topic
        self.status_topic = status_topic
        
        # Storage for received messages
        self._messages = []
        self._message_lock = threading.Lock()
        self._tlm_data = None
        self._tlm_event = threading.Event()
        
        # Create MQTT client
        self.client = mqtt.Client(client_id=f"mep_rfsoc_client_{int(time.time())}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        
        # Connect to broker
        try:
            logging.info(f"Connecting to MQTT broker at {broker}:{port}")
            self.client.connect(broker, port, keepalive=60)
            self.client.loop_start()
            time.sleep(0.5)  # Allow MQTT to settle
        except Exception as e:
            logging.error(f"Failed to connect to MQTT broker: {e}")
            raise
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback for when the client connects to the broker."""
        if rc == 0:
            logging.info(f"Connected to MQTT broker successfully")
            # Subscribe to status topic
            client.subscribe(self.status_topic)
            logging.info(f"Subscribed to {self.status_topic}")
        else:
            logging.error(f"Failed to connect to MQTT broker with code: {rc}")
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback for when the client disconnects from the broker."""
        if rc != 0:
            logging.warning(f"Unexpected MQTT disconnect with code: {rc}")
    
    def _on_message(self, client, userdata, msg):
        """Callback for when a message is received from the broker."""
        try:
            payload_str = msg.payload.decode('utf-8')
            logging.debug(f"Received on {msg.topic}: {BLUE}{payload_str}{RESET}")
            
            with self._message_lock:
                self._messages.append({
                    'topic': msg.topic,
                    'payload': payload_str,
                    'timestamp': time.time()
                })
            
            # If this is a telemetry response, parse and store it
            if msg.topic == self.status_topic:
                try:
                    status_data = json.loads(payload_str)
                    # Check if this is telemetry data
                    if 'state' in status_data or 'f_c_hz' in status_data:
                        self._tlm_data = status_data
                        self._tlm_event.set()
                except json.JSONDecodeError:
                    logging.warning(f"Failed to parse JSON from status topic: {payload_str}")
        except Exception as e:
            logging.error(f"Error in message callback: {e}")
    
    def _send_command(self, task_name, arguments=None):
        """
        Send a command to the RFSoC via MQTT.
        
        Args:
            task_name: Command task name (e.g., "reset", "capture_next_pps", "get tlm")
            arguments: Optional arguments string (e.g., "freq_IF 100")
        
        Examples:
            _send_command("reset")  -> {"task_name": "reset"}
            _send_command("get tlm")  -> {"task_name": "get tlm"}
            _send_command("set", "freq_IF 100")  -> {"task_name": "set", "arguments": "freq_IF 100"}
        """
        command = {"task_name": task_name}
        if arguments is not None:
            command["arguments"] = arguments
        
        command_str = json.dumps(command)
        self.client.publish(self.command_topic, command_str)
        logging.debug(f"Sent: {BLUE}{command_str}{RESET}")
    
    def reset(self):
        """Reset the RFSoC."""
        self._send_command("reset")
        time.sleep(0.5)  # Allow time for RFSoC to reset and reinitialize
    
    def capture_next_pps(self):
        """Trigger capture on the next PPS signal."""
        self._send_command("capture_next_pps")
        time.sleep(0.1)  # Allow time for status update
    
    def set_freq_metadata(self, f_c_hz):
        """
        Set the RF center frequency metadata (used for recorder tagging).
        
        Args:
            f_c_hz: Center frequency in Hz
        """
        self._send_command("set", f"freq_metadata {f_c_hz}")
    
    def set_freq_IF(self, freq_mhz):
        """
        Set the ADC IF frequency (used to configure NCO/mixer) in MHz.
        
        Args:
            freq_mhz: IF frequency in MHz
        """
        self._send_command("set", f"freq_IF {freq_mhz}")
    
    def get_tlm(self, verbose=False, timeout_s=1.5):
        """
        Request and wait for telemetry from the RFSoC.
        
        Args:
            verbose: Enable verbose logging
            timeout_s: Timeout in seconds
            
        Returns:
            Dictionary with telemetry data or None if timeout/error
        """
        # Clear old messages
        with self._message_lock:
            self._messages.clear()
        
        # Clear old telemetry data and event
        self._tlm_data = None
        self._tlm_event.clear()
        
        # Request telemetry - RFSoC expects arguments as list for "get" command
        # Line 131 of start_capture_rx.py checks: if args and args[0] == "tlm"
        # This only works if args is a list like ["tlm"]
        command = {"task_name": "get", "arguments": ["tlm"]}
        command_str = json.dumps(command)
        self.client.publish(self.command_topic, command_str)
        logging.debug(f"Sent: {BLUE}{command_str}{RESET}")
        
        # Wait for telemetry response
        if self._tlm_event.wait(timeout=timeout_s):
            if verbose:
                logging.debug(f"Telemetry received: {self._tlm_data}")
            
            # Validate telemetry - ensure RFSoC is fully initialized
            # f_s should not be NaN, otherwise capture_next_pps will fail
            if self._tlm_data:
                try:
                    f_s = self._tlm_data.get('f_s')
                    # Check if f_s is a valid number (not NaN, not None)
                    if f_s is None or (isinstance(f_s, (int, float)) and (f_s != f_s or f_s == float('inf') or f_s == float('-inf'))):
                        logging.warning("Telemetry received but RFSoC not fully initialized (f_s invalid)")
                        return None
                except:
                    pass
            
            return self._tlm_data
        else:
            logging.warning("Timeout waiting for telemetry")
            return None
    
    def get_msg(self, verbose=True, timeout_s=0.01):
        """
        Get the next message from the message queue.
        
        Args:
            verbose: Enable verbose logging
            timeout_s: How long to wait for a message
            
        Returns:
            Message dictionary or None if no message available
        """
        time.sleep(timeout_s)
        
        with self._message_lock:
            if len(self._messages) > 0:
                msg = self._messages.pop(0)
                if verbose:
                    logging.debug(f"Retrieved message: {BLUE}{msg['payload']}{RESET}")
                return msg
        return None
    
    def set_channel(self, channel):
        """
        Set the RFSoC channel.
        
        Args:
            channel: Channel letter (A, B, C, or D)
        """
        self._send_command("set", f"channel {channel}")
    
    def close(self):
        """Clean up and close the MQTT connection."""
        try:
            self.reset()
        except:
            pass
        
        self.client.loop_stop()
        self.client.disconnect()
        logging.info("MQTT client disconnected")
    
    def __del__(self):
        """Destructor to ensure clean shutdown."""
        try:
            self.close()
        except:
            pass


# Example usage
if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    try:
        # Create RFSoC controller
        rfsoc = MEPRFSoC()
        
        # Reset RFSoC
        print("Resetting RFSoC...")
        rfsoc.reset()
        time.sleep(1)
        
        # Set IF frequency
        print("Setting IF frequency to 100 MHz...")
        rfsoc.set_freq_IF(100.0)
        time.sleep(0.5)
        
        # Set metadata frequency
        print("Setting metadata frequency to 915 MHz...")
        rfsoc.set_freq_metadata(915e6)
        time.sleep(0.5)
        
        # Set channel
        print("Setting channel to A...")
        rfsoc.set_channel("A")
        time.sleep(0.5)
        
        # Capture on next PPS
        print("Triggering capture on next PPS...")
        rfsoc.capture_next_pps()
        time.sleep(2)
        
        # Get telemetry
        print("Requesting telemetry...")
        tlm = rfsoc.get_tlm(verbose=True)
        if tlm:
            print(f"\n{GREEN}Telemetry:{RESET}")
            print(f"  State: {tlm.get('state', 'N/A')}")
            print(f"  Center Freq: {float(tlm.get('f_c_hz', 0))/1e6:.2f} MHz")
            print(f"  IF Freq: {float(tlm.get('f_if_hz', 0))/1e6:.2f} MHz")
            print(f"  Sample Rate: {float(tlm.get('f_s', 0))/1e6:.2f} MHz")
            print(f"  PPS Count: {tlm.get('pps_count', 'N/A')}")
            print(f"  Channels: {tlm.get('channels', [])}")
        else:
            print(f"{RED}Failed to get telemetry{RESET}")
        
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"{RED}Error: {e}{RESET}")
    finally:
        # Clean up
        print("Closing connection...")
        rfsoc.close()
