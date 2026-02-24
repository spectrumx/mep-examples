'''
# Author: John Marino 2026
# Based on /scripts/src/mep_rfsoc.py by Nicholas Rainville
# Based on /opt/git/rfsoc_qsfp_10g/boards/RFSoC4x2/rfsoc_qsfp_offload/scripts/start_capture_rx.py ZMQ->MQTT conversion by Randy Herban 
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
    
    def wait_for_firmware_ready(self, max_wait_s=30):
        """
        Wait for RFSoC firmware to be fully initialized.
        Checks that f_s is not NaN (overlay loaded and sample rate set).
        
        Args:
            max_wait_s: Maximum time to wait in seconds
            
        Returns:
            True if firmware ready, False if timeout
        """
        logging.info("Waiting for RFSoC firmware to initialize...")
        start_time = time.time()
        
        while time.time() - start_time < max_wait_s:
            self._tlm_data = None
            self._tlm_event.clear()
            command = {"task_name": "get", "arguments": ["tlm"]}
            self.client.publish(self.command_topic, json.dumps(command))
            
            if self._tlm_event.wait(timeout=2.0):
                f_s = self._tlm_data.get('f_s') if self._tlm_data else None
                # Check if f_s is valid (not NaN, not None, > 0)
                if f_s is not None and isinstance(f_s, (int, float)) and f_s == f_s and f_s > 0:
                    logging.info(f"RFSoC firmware ready: f_s={f_s/1e6:.2f} MHz")
                    return True
                else:
                    logging.debug(f"RFSoC not ready yet (f_s={f_s}), waiting...")
            else:
                logging.debug("No telemetry response, waiting...")
            
            time.sleep(1)
        
        logging.error(f"RFSoC firmware not ready after {max_wait_s}s! Check RFSoC firmware logs.")
        return False
    
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
            logging.debug(f"MQTT RX on {msg.topic}: {payload_str[:100]}")
            
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
                        logging.debug(f"Got status: state={status_data.get('state')}")
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
        self._tlm_data = None
        self._tlm_event.clear()
        self._send_command("reset")
        # Wait for reset status to arrive before continuing
        self._tlm_event.wait(timeout=1.0)
    
    def capture_next_pps(self):
        """
        Trigger capture on the next PPS signal.
        Waits for 'active' status before returning.
        """
        self._tlm_data = None
        self._tlm_event.clear()
        self._send_command("capture_next_pps")
        
        # Wait for 'active' status - PPS alignment can take up to 1 second
        start_time = time.time()
        while time.time() - start_time < 2.5:
            if self._tlm_event.wait(timeout=0.2):
                if self._tlm_data and self._tlm_data.get('state') == 'active':
                    logging.debug(f"capture_next_pps: got active state")
                    return  # Success
                # Got a status but not active, keep waiting
                self._tlm_event.clear()
        
        logging.warning("capture_next_pps: timeout waiting for active state")
    
    def set_freq_metadata(self, f_c_hz):
        """
        Set the RF center frequency metadata (used for recorder tagging).
        
        Args:
            f_c_hz: Center frequency in Hz
        """
        self._tlm_event.clear()
        self._send_command("set", f"freq_metadata {f_c_hz}")
        self._tlm_event.wait(timeout=0.5)  # Wait for status
    
    def set_freq_IF(self, freq_mhz):
        """
        Set the ADC IF frequency (used to configure NCO/mixer) in MHz.
        
        Args:
            freq_mhz: IF frequency in MHz
        """
        self._tlm_event.clear()
        self._send_command("set", f"freq_IF {freq_mhz}")
        self._tlm_event.wait(timeout=0.5)  # Wait for status
    
    def get_tlm(self, verbose=False, timeout_s=2.0):
        """
        Get telemetry from the RFSoC.
        Returns cached data if available (from capture_next_pps), otherwise requests fresh.
        
        Args:
            verbose: Enable verbose logging
            timeout_s: Timeout in seconds
            
        Returns:
            Dictionary with telemetry data or None if timeout/error
        """
        # If we already have valid telemetry (e.g., from capture_next_pps), return it
        if self._tlm_data is not None:
            logging.debug(f"get_tlm() returning cached: state={self._tlm_data.get('state')}")
            return self._tlm_data
        
        # Otherwise request fresh telemetry
        self._tlm_event.clear()
        command = {"task_name": "get", "arguments": ["tlm"]}
        self.client.publish(self.command_topic, json.dumps(command))
        logging.debug(f"get_tlm() requesting fresh telemetry...")
        
        if self._tlm_event.wait(timeout=timeout_s):
            logging.debug(f"get_tlm() returning: state={self._tlm_data.get('state') if self._tlm_data else None}")
            return self._tlm_data
        else:
            logging.warning("get_tlm() TIMEOUT")
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
        self._tlm_event.clear()
        self._send_command("set", f"channel {channel}")
        self._tlm_event.wait(timeout=1.0)  # Wait for status - channel set triggers initialization
    
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
