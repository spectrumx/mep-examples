#
# mit haystack observator
# rps 9/20/2024
#
# ubxconfig.py
#
# the purpose of this script is to initialize the ublox for the gnss and the MEP
#
"""
 useful commands:

 to verify L1, L2 is seen by gpsd

  $ ubxtool -p RXM-RAWX  | grep "sigId 0"
  $ ubxtool -p RXM-RAWX  | grep "sigId 3"


 to verify uart1 setting:

 $  ubxtool -g CFG-UART1


 to verify uart1 protocol:

 $ ubxtool -g CFG-UART1OUTPROT

to verify TP settings

 $ ubxtool -g CFG-TP


to see the version in use to use in -P:

$ ubxtool -p MON-VER


 functions:
    parse_command_line   - for gpsd configuration options
    SystemCmd            - passes command to command line

"""

import sys
import argparse
import os
import subprocess


#
#-------------------------------------------------------------------
#
def parse_command_line():
    err_f = False
    #
    # Note:
    #       Using the the same argparse variable for the
    #       TCP port and port for serial device and baud rate
    #       will work until both need to be supported at the same time
    #
    parser = argparse.ArgumentParser()

    d_helpStr = "--debug (-d):\t  run in debug mode and not service context."
    parser.add_argument("-d", "--debug",action='store_true', help= d_helpStr)

    e_helpStr = "Set gpsd options for external antenna"
    parser.add_argument("-e", "--external",action='store_true', help= d_helpStr)

    (args,unknowns) = parser.parse_known_args()

    if (len(unknowns) != 0):
      print("unknown options:", unknowns)
      print()
      print(parser._option_string_actions['--debug'].help)       # 1
      print(parser._option_string_actions['--external'].help)    # 2

      print()
      err_f = True

    return err_f,args

# end parse_command_line
#
#
# ------------------------------------------------------------------------------
#
# if this isn't enough, try:
#   https://apollo.haystack.mit.edu/svn/Millstone/
#        infrastructure/software/process_management/ProcessGroup
#
def SystemCmd(cmd,verbose_f=False,shell_f=True):
  err_f = False
  output_str  = ""

  if (verbose_f):
    print("cmd=",cmd)
  try:
     theOutput = subprocess.check_output(cmd,stderr=subprocess.STDOUT,shell=shell_f)
  except Exception as eobj:
     print("Exception: cmd: %s"%(cmd),eobj)
     eobj_output = repr(eobj)
     if (eobj_output != ""):
       print(eobj_output)
     err_f = True
     output_str = eobj_output
  else:
    output_str = theOutput.decode("utf-8")  # new for python3, else b'<text>'
    if (verbose_f):
      print(output_str)
    # end if
  return err_f, output_str
# end SystemCmd
#
#
# ----------------------------------
#     main
# ----------------------------------
#
if __name__ == '__main__':

  err_f = False
  debug_f = False

  err_f, args = parse_command_line()
  if (err_f):
     print("exiting")
     sys.exit()
  else:
    if (debug_f):
      print("external=",args.external)
  # end else

  debug_f = args.debug
  external_f = args.external

  cable_delay = 1   # cable delay in nanoseconds
  drstr = 1         # drive strength is coded
                    #
                    # DRIVE_STRENGTH_2MA  0 2 mA drive strength
                    # DRIVE_STRENGTH_4MA  1 4 mA drive strength
                    # DRIVE_STRENGTH_8MA  2 8 mA drive strength
                    # DRIVE_STRENGTH_12MA 3 12 mA drive strength
                    #
  if (external_f):
    cable_delay = 50
    drstr =3

  os.environ["UBXOPTS"] = "-f /dev/ttyUBLOX -P 29.20"

  #
  # note: gary miller is the difficult human who holds responsibility over gpsd open source
  #
  cmdList = ["ubxtool -e BINARY ",    # 1st
             "ubxtool -d NMEA   ",    # 2nd
             "ubxtool -d BEIDOU ",
             "ubxtool -d GALILEO",
             "ubxtool -d GLONASS",
             "ubxtool -d SBAS   ",
             "ubxtool -e GPS    ",
             "ubxtool -e RAWX   ",
             "ubxtool -e SFRBX  ",
             "ubxtool -z CFG-UART1-BAUDRATE,3686400 ", # the fastest the rp2040 can handle
             "ubxtool -z CFG-UART1-DATABITS,0     ",
             "ubxtool -z CFG-UART1OUTPROT-NMEA,1  ",
             "ubxtool -z CFG-UART1OUTPROT-UBX,0   ",
             "ubxtool -z CFG-UART1OUTPROT-RTCM3X,0",
             "ubxtool -z CFG-TP-TP1_ENA,1         ",
             "ubxtool -z CFG-TP-TP2_ENA,1         ",
             "ubxtool -z CFG-TP-LEN_TP1,100       ",  # makes pps visible
             "ubxtool -z CFG-TP-LEN_LOCK_TP1,1000 ",
             "ubxtool -z CFG-TP-LEN_TP2,100       ",
             "ubxtool -z CFG-TP-LEN_LOCK_TP2,1000 ",
             "ubxtool -z CFG-TP-ANT_CABLEDELAY,%d "%(cable_delay),  # antenna
             #"ubxtool -z CFG-TP-DRSTR_TP1,%d"%(drstr),              # coded driver strength
             #"ubxtool -z CFG-TP-DRSTR_TP2,%d"%(drstr),
         ]

  ii = 0

  while (ii < len(cmdList)):

    err_f,resp = SystemCmd(cmdList[ii],verbose_f=True)

    if (err_f):
      print("error at command %d, exiting"%(ii+1))
      break
    else:
      if (len(resp)>0):
        print("resp=",resp)

    ii = ii + 1
  # end while

  if (not err_f):
     print("no errors detected")

#
# ----------------------------------
#     END OF FILE
# ----------------------------------
#






