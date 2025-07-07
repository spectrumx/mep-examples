#
#!/usr/bin/env python3
#
# afe.py
#
# MIT Haystack Observatory
# Ben Welchman 07-01-2025
#

# --------------------------
#
# List of Functions:
#
# --------------------------

import socket
import sys
import argparse

SOCKET_PATH = "/tmp/afe_service.sock"

def send_command(block, channel, addr, bit):

    msg = f"{block} {channel} {addr} {bit}"
    command = msg.encode('utf-8')
    
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
      s.connect(SOCKET_PATH)
      s.sendall(command)
      reply = s.recv(1024).decode('utf-8', errors='ignore').strip()
      print(reply)

    return 


def print_help():

  msg = "\nRUN ONLY ONE COMMAND AT A TIME\n\n"

  msg = "\nTools:\n\n"
  msg = msg + ">> afe.py  [-h]  [-p]  [-l]  [-r <logging rate>] \n\n"
  msg = msg + "  -h, --help         Show this help message and exit\n"
  msg = msg + "  -p, --print        Print the current telemetry and register states\n"
  msg = msg + "  -l, --log          Log the current telemetry and register states\n"
  msg = msg + "  -r, --rate         Select period of telemetry logging (in seconds)"
  
  msg = msg + "\nShortcuts:\n\n"
  msg = msg + ">> afe.py  [-a <0/1>]  [-i <channel> <0,1>]\n\n"
  msg = msg + "  -h, --help         Show this help message and exit\n"
  msg = msg + "  -a, --antenna      GNSS Antenna Select <0/1> (Internal/External)\n"
  msg = msg + "  -i, --inputrf      RF Input select <channel> (1,2,3,4) <0/1/> (Internal/External)\n"

  msg = msg + "\nManual Reguster Programming:\n\n"
  msg = msg + ">> afe.py  <block>  <addr>  <value>\n"
  msg = msg + ">> afe.py  -rx2  9  1\n\n"
  msg = msg + "  <block>\n"
  msg = msg + "     -m, --main           Main register (trigs, ebias, pps sel, ref sel, antenna sel)\n"
  msg = msg + "     -tx1, tx2            TX registers (blank sel, filter bypass)\n"
  msg = msg + "     -rx1, rx2, rx2, rx4  RX registers (chan bias, rf trig, filter byp, amp byp, atten)\n\n"

  msg = msg + "  <addr>\n"
  msg = msg + "     Register address: 0-9\n\n"

  msg = msg + "  <value>\n"
  msg = msg + "     Register value to set: 0,1\n"
  
  print(msg)


def parse_command_line():
    
    error_flag = False

    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument("-h", "--help", action="store_true", dest="help_flag")
    parser.add_argument("-p", "--print", action="store_true")
    parser.add_argument("-l", "--log", action="store_true")
    parser.add_argument("-r", "--rate", nargs=1)

    parser.add_argument("-a", "--antenna", choices=["0","1"])
    parser.add_argument("-i", "--inputrf", nargs=2, choices=["0","1","2","3","4"]) 

    parser.add_argument("-m", "--main", nargs=2, choices=["0","1","2","3","4","5","6","7","8","9"])
    parser.add_argument("-tx1", nargs=2, choices=["0","1","2","3","4","5","6","7","8","9"])
    parser.add_argument("-tx2", nargs=2, choices=["0","1","2","3","4","5","6","7","8","9"])

    parser.add_argument("-rx1", nargs=2, choices=["0","1","2","3","4","5","6","7","8","9"])
    parser.add_argument("-rx2", nargs=2, choices=["0","1","2","3","4","5","6","7","8","9"])
    parser.add_argument("-rx3", nargs=2, choices=["0","1","2","3","4","5","6","7","8","9"])
    parser.add_argument("-rx4", nargs=2, choices=["0","1","2","3","4","5","6","7","8","9"])


    (args,unknowns) = parser.parse_known_args()

    if args.help_flag:
      print_help()
      sys.exit()

    if (len(unknowns) != 0):
      print("Unknown options:", unknowns)
      print_help()
      error_flag = True

    return error_flag, args


def update_reg_states(args):

  # block
  #   0: program main reg
  #   1: program tx reg
  #   2: program rx reg
  #   3: print state
  #   4: log state
  #   5: change logging rate

  if args.print:
    block = 3
    channel = -1
    addr = -1
    value = -1

  if args.log:
    block = 4
    channel = -1
    addr = -1
    value = -1

  if args.rate:
    block = 5
    print(args.rate)
    channel = int(args.rate[0])
    addr = -1
    value = -1

  if args.main:
    block = 0
    channel = -1
    addr_str, value_str = args.main
    addr = int(addr_str)
    value = int(value_str)

  if args.tx1:
    block = 1
    channel = 1
    addr_str, value_str = args.tx1
    addr = int(addr_str)
    value = int(value_str)
  
  if args.tx2:
    block = 1
    channel = 2
    addr_str, value_str = args.tx2
    addr = int(addr_str)
    value = int(value_str)

  if args.rx1:
    block = 2
    channel = 1
    addr_str, value_str = args.rx1
    addr = int(addr_str)
    value = int(value_str)

  if args.rx2:
    block = 2
    channel = 2
    addr_str, value_str = args.rx2
    addr = int(addr_str)
    value = int(value_str)
    
  if args.rx2:
    block = 2
    channel = 3
    addr_str, value_str = args.rx3
    addr = int(addr_str)
    value = int(value_str)

  if args.rx2:
    block = 2
    channel = 4
    addr_str, value_str = args.rx4
    addr = int(addr_str)
    value = int(value_str)

  if args.antenna:
    block = 0
    channel = -1
    addr = 9
    value = int(args.antenna)
  
  if args.inputrf:
    block = 2
    addr = 1
    channel_str, value_str = args.inputrf
    channel = int(channel_str)
    value = 1 - int(value_str)

  return block, channel, addr, value


def main():
   
  error_flag, args = parse_command_line() # parse command line options
   
  if (error_flag):
    sys.exit()

  if (len(sys.argv) > 1):
    block, channel, addr, value = update_reg_states(args)
    send_command(block, channel, addr, value)
  else:
    print("No command given. List of commands:")
    print_help()

  sys.exit()


if __name__ == '__main__':

  main()