## Remote command script for RFSoC 
import zmq
import argparse
import time
import logging

def main(args):
    context = zmq.Context()
    pub_socket = context.socket(zmq.PUB)
    pub_socket.bind("tcp://*:60200")  # Make sure the subscriber connects to the same address

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
    finally:
        pub_socket.close()
        context.term()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Send command to the RFSoC')
    parser.add_argument('command', type=str, help='Command string', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    main(args)