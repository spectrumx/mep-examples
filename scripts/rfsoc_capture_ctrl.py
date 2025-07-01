#!/opt/radiohound/python313/bin/python

"""
rfsoc_capture_ctrl.py
Remote command script for controlling an RFSoC via ZeroMQ.

Author: nicholas.rainville@colorado.edu
"""

import zmq
import argparse
import time
import logging
import threading

def poll_subscriber(sub_socket, poller, shutdown_flag):
    """
    Thread to print RFSoC telemetry

    """

    while not shutdown_flag.is_set():
        socks = dict(poller.poll(10))  # Poll every 10ms
        if sub_socket in socks and socks[sub_socket] == zmq.POLLIN:
            message = sub_socket.recv_string()
            print(f"\nReceived: {message}")
        time.sleep(0.001)  # Optional, prevents excessive CPU usage

def main(args):
    """
    Main function for the capture control script.

    This function initializes a ZeroMQ publisher socket and sends commands to
    an RFSoC device. It can either accept user input commands or send a pre-defined
    command string.

    Args:
        args (argparse.Namespace): Command-line arguments. Expected to contain:
            - command (list): A list of command strings to send.

    Raises:
        KeyboardInterrupt: If the user interrupts the program (Ctrl+C).
    """
    context = zmq.Context()
    pub_socket = context.socket(zmq.PUB)
    pub_socket.bind("tcp://*:60200")  # Make sure the subscriber connects to the same address

    sub_socket_str = "tcp://192.168.20.100:60201"
    sub_socket = context.socket(zmq.SUB)
    sub_socket.connect(sub_socket_str)
    sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")
    poller = zmq.Poller()
    poller.register(sub_socket, zmq.POLLIN)

    shutdown_flag = threading.Event()
    thread = threading.Thread(target=poll_subscriber, args=(sub_socket, poller, shutdown_flag))
    thread.start()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logging.info("RFSoC Capture Control")

    try:
        command_str = " ".join(args.command)
        if (command_str == ""):
            print("Type commands and hit Enter to send. Ctrl+C to exit.\n")

            while True:
                user_input = input("Enter command: ")
                if user_input.strip() == "":
                    continue
                pub_socket.send_string(f"{user_input}")
                logging.info(f"Sent: {user_input}")
        else:
            time.sleep(.2) # Wait for ZMQ to initialize
            pub_socket.send_string(f"{command_str}")
            logging.info(f"Sent: {command_str}")

    except KeyboardInterrupt:
        logging.info("\nExiting RFSoC Capture Control")
        shutdown_flag.set()

    finally:
        shutdown_flag.set()
        thread.join()
        pub_socket.close()
        sub_socket.close()
        context.term()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Send command to the RFSoC')
    parser.add_argument('command', type=str, help='Command string', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    main(args)
