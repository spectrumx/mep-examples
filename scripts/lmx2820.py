#

# mit haystack observatory

# rps 10/25/2024

#

# lmx2820.py

#

# code from https://forums.raspberrypi.com/viewtopic.php?t=319702
# the original code was incomplete, unverified, and required
# reverse engineering for missing functions

#

"""
  SystemCmd       - run a shell command

  testBit         - test bit

  setBit          - set bit

  clearBit        - clear bit

  toggleBit       - toggle bit

  modifyBM        - original code reference not available, reverse engineered

  BMofNumber      - original code reference not available, reverse engineered

  parseBytes      - original code reference not available, reverse engineered



  class:

    LMX2820

    functions:

     __init__

     LMX2820InitRegs

     LMX2820calcIntNumDen

     LMX2820setREGSforNewFreq

     LMX2820WriteSingeRegister

     LMX2820StartUp

     LMX2820ChangeFreq



"""



import sys
import os
import subprocess
import time

import fractions




SIMU = False     # set True for debugging apart from hardware
MAX_PWR = 7
MIN_PWR = 0

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
# ------------------------------------------------------------------------------
#

#

# exception raised if environment variable not set

#
err_f = False
retry_f = False

try:

  devid = os.environ['BLINKA_FT232H']

except Exception as obj:
  err_f = True

  print("attempting to set 'export BLINKA_FT232H=1' ")

  err_f,resp = SystemCmd('export BLINKA_FT232H=1',verbose_f=True)

  if (err_f):
    print("something went wrong: %s, exiting early"),resp
    sys.exit()
  else:
    retry_f = True # signal retry
   
# end try/except block

if (retry_f):
  try:

    devid = os.environ.get('BLINKA_FT232H')  # resolves weird bug

  except Exception as obj:
    print("need to set 'export BLINKA_FT232H=1' on the command line ")
    raise
  # end try/except
# end if possibly recoverable error
#
# ------------
# still here?
# ------------
#

if (devid != '1'):

  print("need to set 'export BLINKA_FT232H=1', value found=%s"%(devid))

  print("exiting early")

  sys.exit()

# endif unexpected id

#
# ------------
# still here?
# ------------

#

# exception raised if board not plugged in

#

try:

  import board

except Exception as eobj:

  if (SIMU):

    pass

  else:

    print("Exception on 'import board\n",eobj)

    print("exiting early")

    sys.exit()

# end except



try:

  import busio

except Exception as eobj:

  if (SIMU):

    pass

  else:

    print("Exception on 'import busio\n",eobj)

    print("exiting early")

    sys.exit()

# end except



try:

  from digitalio import DigitalInOut

except Exception as eobj:

  if (SIMU):

    pass

  else:

    print("Exception on 'from digitalio import DigitalInOut\n",eobj)

    print("exiting early")

    sys.exit()

# end except







# from pyftdi.spi import SpiController



#

#------------------------------------------------------------------------------

#

#  GLOBALS

#


spi = None
CSpin = None





#

#------------------------------------------------------------------------------

#

# https://wiki.python.org/moin/BitManipulation

#

# testBit() returns a nonzero result, 2**offset, if the bit at 'offset' is one.



def testBit(in_val, offset):

      retval = 0

      debug_f = False



      mask = 1 << offset

      if (debug_f):

        print("mask=",mask)

      # endif debug



      test_val = in_val & mask

      if (debug_f):

        print("test_val=", test_val)

      # endif debug



      return test_val

# end testBit

#

#------------------------------------------------------------------------------

#

# setBit() returns an integer with the bit at 'offset' set to 1.



def setBit(int_type, offset):

   mask = 1 << offset

   return(int_type | mask)

# end setBit

#

#------------------------------------------------------------------------------

#

# clearBit() returns an integer with the bit at 'offset' cleared.



def clearBit(int_type, offset):

    mask = ~(1 << offset)

    return(int_type & mask)

# end clearBit

#

#------------------------------------------------------------------------------

#

# toggleBit() returns an integer with the bit at 'offset' inverted, 0 -> 1 and 1 -> 0.



def toggleBit(int_type, offset):

    mask = 1 << offset

    return(int_type ^ mask)

# end toggleBit

#

#------------------------------------------------------------------------------

#

def modifyBM(dst,       # destination register

             src,       # source value

             ofs,       # bit offset

             width):     # number of bits to use in src



  debug_f = False



  not_mask = 0xFFFFFFFF  # 32 bits



  # create bit mask

  ii = 0

  mask = 0

  while (ii < width):

    mask = mask | setBit(1,ii)

    ii = ii + 1

  # end while



  if (debug_f):

    print("mask=",mask)

  # endif debug



  # create shifted field



  field = src << ofs



  # shift mask



  ofs_mask = mask << ofs



  if (debug_f):

    print("ofs_mask=", '{0:016b}'.format(ofs_mask))

  # endif debug



  # invert mask (can't use ~ because this does 2's complement



  ofs_mask = ofs_mask ^ not_mask



  if (debug_f):

    print("ofs_mask=", '{0:016b}'.format(ofs_mask))

  # endif debug





  if (debug_f):

    print("ofs_mask=", '{0:016b}'.format(ofs_mask))

  # endif debug



  # clear bits in register



  dst = dst & ofs_mask



  # place bits in register



  dst = dst | field



  return dst



# end modifyBM

#

# ----------------------------------

#

def BMofNumber(src,bidx,count):

  dst = 0

  debug_f = False



  ii = 0

  while (ii < count):



    if (debug_f):

      print("dst=",hex(dst))

    # endif



    test_bit = testBit(src,bidx+ii)



    if (debug_f):

      print("test_bit=", test_bit)



    if (test_bit > 0):

      dst = setBit(dst, ii)

      if (debug_f):

        print("bit=",hex(test_bit))

      # endif debug



    # endif



    ii = ii + 1



  # end while



  if (debug_f):

    print("dst=",hex(dst))

  # endif



  return dst



# end BMofNumber

#

# ----------------------------------

#

def parseBytes(theData,count):

    """Splits bytes into high and low bytes."""

    ret_bytes = bytearray()



    ii = 0

    while (ii < count):



      elem = theData & 0xff



      ret_bytes.append(elem)



      theData = theData >> 8



      ii = ii + 1



    return ret_bytes

# end parseBytes

#

#------------------------------------------------------------------------------

#

#RegFREQ is the OSCin Freq

#RefDoubler: 0 = bypassed, 1 = enabled

#Ref Multipler: 1 = bypassed, 3,5,7 only allowed

#Ref Pre-Divider: 1 = bypass max = 128

#Ref Post-Divider: 1= bypass max = 255

class LMX2820:

    def __init__(self, RefFREQ, RefDoubler, RefMultipler, PreRDiv, PostRDiv):

        self.RefFREQ = RefFREQ

        self.RefDoubler = RefDoubler

        self.RefMultipler = RefMultipler

        self.PreRDiv = PreRDiv

        self.PostRDiv = PostRDiv

        self.reg = [0]*123

        self.DividerLUT = [2,4,8,16,32,64,128]


        self.reg[0] = 0b0110010001110000     #Reset register PFD = 150 MHz Setting Fcal HPFD and LPFD ADJ. F_Cal is 1


        self.reg[2] = 0b1011001111101000     #Setting clock Div value

        self.reg[11] = 0b0000011000000010    #Ref doubler bypassed

        self.reg[12] = 0b0000010000001000    #Ref multiplier bypassed

        self.reg[13] = 0b0000000000111000    #Ref Post R Divider set to 1

        self.reg[14] = 0b0011000000000001    #Ref Pre R Divider set to 1

        if(self.RefDoubler != 1 and self.RefDoubler != 0):                                                     #CHekcing value of OSC_2x

            self.RefDoubler = 0                                                                                #Ensuring only states 0 and 1 are possible

        if(self.RefMultipler!=1 and self.RefMultipler!=3 and self.RefMultipler!=5 and self.RefMultipler>7):    #Testing if requested multiplcation factor is valid

            print("Ref multiplier out of range (1,3,5,7 allowed). set to bypass, 1")

            self.RefMultipler = 1                                                                              #If value is out of range set to bypass

        if(self.PreRDiv<1 or self.PreRDiv>4095):                                                               #Testing if requested pre-divider value is valid

            print("Ref pre divider out  of range (1-128). Set to bypass, 1")

            self.PreRDiv = 1                                                                                   #If out of range, set to bypass

        if(self.PostRDiv<1 or self.PostRDiv>255):                                                              #Checking if post divider is in valid range

            print("Ref pre divider out  of range (1-128). Set to bypass, 1")

            self.PostRDiv = 1                                                                                   #If out of range, set to bypass

        Fpfd = self.RefFREQ * (((1+self.RefDoubler)*self.RefMultipler) / (self.PreRDiv*self.PostRDiv))          #Calculating Phase Frequency detector frequency

        if (Fpfd > 225e6 or Fpfd < 5e6):                                                                        #If Fpfd is out of range, set reference chain to bypass all

            print("Phase frequency detector frequency too high. Must be <=225 MHz and  >=5 MHz. Setting multiplier and dividres to bypass.")

            self.RefDoubler == 0

            self.RefMultipler = 1

            self.PreRDiv = 1

            self.PostRDiv = 1

        else:                                                                  #If the Fpfd is value, set the registers for the the refernece chain

            if (self.RefDoubler == 1):

                self.reg[11] = modifyBM(self.reg[11],1,4,1)                    #Adjusting register 11 accordingly for the Ref multiply by 2

            self.reg[12] = modifyBM(self.reg[12],self.RefMultipler,10,3)       #Setting new Ref Multipleir value

            self.reg[14] = modifyBM(self.reg[14],self.PreRDiv,0,12)            #Setting new Ref pre divider value

            self.reg[13] = modifyBM(self.reg[13],self.PostRDiv,5,8)            #Setting new Ref post divider value

            if(Fpfd <= 100e6):                                                 #Setting the FCal_Hpfd_ADJ according to the Fpfd

                self.reg[0] = modifyBM(self.reg[0],0,9,2)                      #

            elif(100e6 < Fpfd and Fpfd <150e6):                               #

                self.reg[0] = modifyBM(self.reg[0],1,9,2)                      #

            elif(150e6 < Fpfd and Fpfd <=200e6):                               #

                self.reg[0] = modifyBM(self.reg[0],2,9,2)                      #

            elif(200e6 < Fpfd):                                                #

                self.reg[0] = modifyBM(self.reg[0],3,9,2)                      #

            if(Fpfd >= 10e6):                                                  #Setting FCal_Lpfd_ADJ according to Fpfd

                self.reg[0] = modifyBM(self.reg[0],0,7,2)                      #

            elif(10e6 > Fpfd and Fpfd >= 5e6):                                 #

                self.reg[0] = modifyBM(self.reg[0],1,7,2)                      #

            elif(5e6 > Fpfd and Fpfd >= 2.5e6):                                #

                self.reg[0] = modifyBM(self.reg[0],2,7,2)                      #

            elif(2.5e6 > Fpfd):                                                #

                self.reg[0] = modifyBM(self.reg[0],3,7,2)                      #

            if (self.RefFREQ <= 200e6):                                        #Setting Cal_Clk_Divider based on input reference frequency

                self.reg[2] = modifyBM(self.reg[2],0,12,3)                     #

            elif(200e6 < self.RefFREQ and self.RefFREQ <= 400e6):              #

                self.reg[2] = modifyBM(self.reg[2],1,12,3)                     #

            elif(400e6 < self.RefFREQ and self.RefFREQ <= 800e6):              #

                self.reg[2] = modifyBM(self.reg[2],2,12,3)                     #

            elif(800e6 < self.RefFREQ):                                        #

                self.reg[2] = modifyBM(self.reg[2],3,12,3)                     #

# end __init__

#

# ----------------------------------

#

#Function to intilaize the registesr for the LMX and the CE pin for SPI

#Still needs to be transmitted to chip properly

#Commented registerd get defined in class defintion

def LMX2820InitRegs(LMX):

    #LMX.reg[0] = 0b0100000001110000     #Reset register PFD = 100 MHz Setting Fcal HPFD and LPFD ADJ.

    LMX.reg[1] = 0b0101011110100000     #Enable if doubler is engaged

    #LMX.reg[2] = 0b1011001111101000     #Setting clock Div value

    LMX.reg[3] = 0x41                   #Reserved

    LMX.reg[4] = 0x4204                 #Reserved

    LMX.reg[5] = 0x32                   #Reserved        Reset:0x3832

    LMX.reg[6] = 0xA43                  #ACAL_CMP_DLY

    LMX.reg[7] = 0x00                   #Reserved        Reset:0xC8

    LMX.reg[8] = 0xC802                 #Reserved

    LMX.reg[9] = 0x05                   #Reserved

    LMX.reg[10] = 0x00                  #Manual PFD_DLY     Reset: 0b0000011000000011

    ##LMX.reg[11] = 0b0000011000000010    #Ref doubler bypassed

    ##LMX.reg[12] = 0b0000010000001000    #Ref multiplier bypassed

    ##LMX.reg[13] = 0b0000000000111000    #Ref Post R Divider set to 1

    ##LMX.reg[14] = 0b0011000000000001    #Ref Pre R Divider set to 1

    LMX.reg[15] = 0b0010000000000001    #Setting negative polairty for PFD charge pump to nomral operation

    LMX.reg[16] = 0b0001011100011100    #Setting charge pump current   Reset: 0b0010011100011100

    LMX.reg[17] = 0b0001010111000000    #Setting lock det type to continious  Reset: 1010001000000

    LMX.reg[18] = 0x3E8                 #Lock Detect assertion delay

    LMX.reg[19] = 0b0010000100100000    #Disabling temperature sensor

    LMX.reg[20] = 0b0010011100101100    #VCO_DACISET

    LMX.reg[21] = 0x1C64                #Reserved

    LMX.reg[22] = 0xE2BF                #Seting VCO core 7 as core to Cal

    LMX.reg[23] = 0b0001000100000010    #Disabling VCO_SEL_Force

    LMX.reg[24] = 0xE34                 #Reserved

    LMX.reg[25] = 0x624                 #Reserved

    LMX.reg[26] = 0xDB0                 #Reserved

    LMX.reg[27] = 0x8001                #Reserved

    LMX.reg[28] = 0x639                 #Reserved

    LMX.reg[29] = 0x318C                #Reserved

    LMX.reg[30] = 0xB18C                #Reserved

    LMX.reg[31] = 0x401                 #Reserved

    LMX.reg[32] = 0b0001000000000001    #Setting Both Output vhannel dividers to 0 (Div by 2)

    LMX.reg[33] = 0x00                  #Reserved

    LMX.reg[34] = 0b0000000000010000    #Disabling External VCO and LoopBack

    LMX.reg[35] = 0b0011000100000000    #Enable Mash Reset and Set MASH order

    LMX.reg[36] = 60                    #Setting INT value

    LMX.reg[37] = 0x500                 #Setting PFD_DEL, only active whe PFD_DLY_MANUAL is 1, which it's NOT

    LMX.reg[38] = 0x00                  #MSBs PLL_DEN

    LMX.reg[39] = 0x3E8                 #LSBs of PLL_DEN

    LMX.reg[40] = 0x00                  #MSBs MASH seed

    LMX.reg[41] = 0x00                  #LSBs MASH seed

    LMX.reg[42] = 0x00                  #MSBs PLL_NUM

    LMX.reg[43] = 0x00                  #LSBs PLL_NUM

    LMX.reg[44] = 0x00                  #MSBs INSTCAL_PLL  2^32*(PLL_NUM/PLL_DEN)

    LMX.reg[45] = 0x00                  #LSBs INSTCAL_PLL

    LMX.reg[46] = 0x300                 #Reserved

    LMX.reg[47] = 0x300                 #Reserved

    LMX.reg[48] = 0x4180                #Reserved

    LMX.reg[49] = 0x00                  #Reserved

    LMX.reg[50] = 0x80                  #Reserved

    LMX.reg[51] = 0x203F                #Reserved

    LMX.reg[52] = 0x00                  #Reserved

    LMX.reg[53] = 0x00                  #Reserved

    LMX.reg[54] = 0x00                  #Reserved

    LMX.reg[55] = 0x02                  #Reserved

    LMX.reg[56] = 0x01                  #Reserved

    LMX.reg[57] = 0x01                  #Disabling PFDIN input

    LMX.reg[58] = 0x00                  #Reserved

    LMX.reg[59] = 0x1388                #Reserved

    LMX.reg[60] = 0x1F4                 #Reserved

    LMX.reg[61] = 0x3E8                 #Reserved

    LMX.reg[62] = 0x00                  #MSBs MASH_RST_COUNT

    LMX.reg[63] = 0xC350                #LSBs MASH_RST_COUNT

    LMX.reg[64] = 0b0000000010000000    #Setting System Ref divider

    LMX.reg[65] = 0x00                  #Setting System Ref divider

    LMX.reg[66] = 0x3F                  #Setting 2 of 4 JESD registers, sum of 2 registers must be equal or greater than 63

    LMX.reg[67] = 0x00                  #Setting last 2 JESD regiseres as  zero

    LMX.reg[68] = 0x00                  #Disabling Phase Sync

    LMX.reg[69] = 0x11                  #Power Down System Refoutput buffer

    LMX.reg[70] = 0x0E                  #Not setting Registers double buffered, Changes to regs will only be enabled after a write to R0

    LMX.reg[71] = 0b1000100000000001    # Read Only, captured from 'pro' programmer

    LMX.reg[72] = 0b0000000000001000    # Read Only, captured from 'pro' programmer

    LMX.reg[73] = 0b1001110101111101    # Read Only, captured from 'pro' programmer

    LMX.reg[74] = 0b1000011100101001    # Read Only, captured from 'pro' programmer

    LMX.reg[75] = 0b0001000100101010    # Read Only, captured from 'pro' programmer

    LMX.reg[76] = 0x00                  #Reserved

    LMX.reg[77] = 0b0000011000001000    #Mute Pin Polarity

    LMX.reg[78] = 0x01                  #Output A enabled and OutA Mux set to VCO

    LMX.reg[79] = 0x11E                 #Power Down B, OutB Mux set to VCO, Max power outputA

    LMX.reg[80] = 0x1C0                 #Output B max power

    LMX.reg[81] = 0x00                  #Reserved

    LMX.reg[82] = 0x00                  #Reserved

    LMX.reg[83] = 0xF00                 #Reserved

    LMX.reg[84] = 0x40                  #Reserved

    LMX.reg[85] = 0x00                  #Reserved

    LMX.reg[86] = 0x40                  #Reserved

    LMX.reg[87] = 0xFF00                #Reserved

    LMX.reg[88] = 0x3FF                 #Reserved

    LMX.reg[89] = 0x00                  #Reserved

    LMX.reg[90] = 0x00                  #Reserved

    LMX.reg[91] = 0x00                  #Reserved

    LMX.reg[92] = 0x00                  #Reserved

    LMX.reg[93] = 0x1000                #Reserved

    LMX.reg[94] = 0x00                  #Reserved

    LMX.reg[95] = 0x00                  #Reserved

    LMX.reg[96] = 0x17F8                #Reserved

    LMX.reg[97] = 0x00                  #Reserved

    LMX.reg[98] = 0x1C80                #Reserved

    LMX.reg[99] = 0x19B9                #Reserved

    LMX.reg[100] = 0x533                #Reserved

    LMX.reg[101] = 0x3E8                #Reserved

    LMX.reg[102] = 0x28                 #Reserved

    LMX.reg[103] = 0x14                 #Reserved

    LMX.reg[104] = 0x14                 #Reserved

    LMX.reg[105] = 0x0A                 #Reserved

    LMX.reg[106] = 0x00                 #Reserved

    LMX.reg[107] = 0x00                 #Reserved

    LMX.reg[108] = 0x00                 #Reserved

    LMX.reg[109] = 0x00                 #Reserved

    LMX.reg[110] = 0x1F                 #Reserved

    LMX.reg[111] = 0x00                 #Reserved

    LMX.reg[112] = 0xFFFF               #Reserved

    LMX.reg[113] = 0b1000101000110110   # captured from 'pro' programmer

    LMX.reg[114] = 0b1100000000011010   # captured from 'pro' programmer

    LMX.reg[115] = 0b0011010110001010   # captured from 'pro' programmer

    LMX.reg[116] = 0b1111011001010101   # captured from 'pro' programmer

    LMX.reg[117] = 0x00                 #Reserved

    LMX.reg[118] = 0x00                 #Reserved

    LMX.reg[119] = 0b0000000000001001   # captured from 'pro' programmer

    LMX.reg[120] = 0x00                 #Reserved

    LMX.reg[121] = 0x00                 #Reserved

    LMX.reg[122] = 0x00                 #Reserved



# end LMX2820InitRegs

#

# ----------------------------------

#

#Calculating hew INT NUM and DEN values for a new frequency request within VCO range

#VCO range limits 5.65 - 11.3 GHz

def LMX2820calcIntNumDen(LMX,newFreq):

    #newParams = fractions()

    Fpfd = LMX.RefFREQ * (((1+LMX.RefDoubler)*LMX.RefMultipler) / (LMX.PreRDiv*LMX.PostRDiv))  #Calculating Phase Frequency detector frequency

    #print("PFD frequency: ",Fpfd)

    if newFreq < 5.56e9 or newFreq > 11.3e9:                                                   #Testing if requested frequency falls within VCO range

        INT = 60                                                                               #If not, set the output values for default frequency of 6 GHz

        NUM = 0

        DEM = 1

        print("Frequency outside VCO range")

    else:

        """

        createdReducedFraction(newParams,newFreq/Fpfd)

        INT = newParams.WHOLE

        NUM = newParams.NUM

        DEM = newParams.DEM

        """

        #print("newFreq,Fpfd=",newFreq,Fpfd)                                      # diagnostic, params into next fcn must be int()

        newParams = fractions.Fraction(int(newFreq),int(Fpfd))

        INT = newParams.__round__()

        NUM = newParams.numerator

        DEM = newParams.denominator

    RFout = Fpfd * (INT + (NUM/DEM))                                              #Calculating the real RF frequency out

    return [INT,NUM, DEM,RFout]

# end LMX2820calcIntNumDen

#

# ----------------------------------

#

#Setting the registers for a new frequency request

#Sets dividers and mux for frequency requests down to 44.140265 MHz

#Max frequency 22.6 GHz.

def LMX2820setREGSforNewFreq(LMX,newFreq,newPwr=MAX_PWR):



    global SIMU


    NewPwrValue = newPwr<<1
    NewPwrValue = newPwr | 0x0110
    LMX.reg[79] = NewPwrValue


    outputDividerIndex = 0                                                 #Defining an index to use for output divider

    if newFreq < 44.140265e6 or newFreq > 22.6e9:                          #Making sure the requested freuqency falls withing accetable range

        realFreq = 6e9                                                     #If not, defining the real freuency as 6 GHz

        LMX.reg[78] = modifyBM(LMX.reg[78],1,0,2)                          #Setting output_A mux to VCO

        LMX.reg[1] = modifyBM(LMX.reg[1],0,1,1)                            #Setting InstaCal_2x to normal operation

    elif newFreq >= 5.56e9 and newFreq <= 11.3e9:                          #If the requested freuqnecy falls withing the VCO range,

        realFreq = newFreq                                                 #define the real ferquency as the reqeusted frequency

        LMX.reg[78] = modifyBM(LMX.reg[78],1,0,2)                          #Setting output_A mux to VCO

        LMX.reg[1] = modifyBM(LMX.reg[1],0,1,1)                            #Setting InstaCal_2x to normal operation

    elif newFreq >11.3e9 and newFreq <= 22.6e9:                            #If the requested frequency falls within 2x VCO range,

        realFreq = newFreq/2                                               #Set realFreq as half desired frequency

        LMX.reg[78] = modifyBM(LMX.reg[78],2,0,2)                          #Setting output_A mux to doubler

        LMX.reg[1] = modifyBM(LMX.reg[1],1,1,1)                            #Setting InstaCal_2x to doubler engaged operation

    else:                                                                  #If the requested freuqnecy needs the divider outputs

        LMX.reg[78] = modifyBM(LMX.reg[78],0,0,2)                          #Set output_A mux to Channel Divider

        while newFreq*LMX.DividerLUT[outputDividerIndex]<5.56e9:            #Loop through the divider lookup table to find the necessary divider to reach the VCO range

            outputDividerIndex=outputDividerIndex+1

        realFreq = newFreq*LMX.DividerLUT[outputDividerIndex]              #Define the real frequency as the requested frequency * the necessary divider

        LMX.reg[32] = modifyBM(LMX.reg[32],outputDividerIndex,6,3)         #Setting VCO divider value

        LMX.reg[1] = modifyBM(LMX.reg[1],0,1,1)                            #Setting InstaCal_2x to normal operation

#    print(newFreq,realFreq, outputDividerIndex,BMofNumber(LMX.reg[78],0,2))    #Debug

    newParams = LMX2820calcIntNumDen(LMX,realFreq)                         #Calculating new INT, NUM, DEN for the real frequency

#    print("INT, NUM, DEM, RealFreq")

#    print(newParams)                                                       #Debug

    if newParams[1] == 0:

        LMX.reg[35] = modifyBM(LMX.reg[35],0,7,2)                          #Seting Mash_Order to zero if system in is Interger mode (NUM = 0)

        LMX.reg[35] = modifyBM(LMX.reg[35],0,12,1)                          #Seting Mash_Reset to zero if system in is Interger mode (NUM = 0)

    elif newParams[2] <=7 and newParams[1] != 0:

        LMX.reg[35] = modifyBM(LMX.reg[35],1,7,2)                          #Seting MASH modulator to first order if DEN < 7

        LMX.reg[35] = modifyBM(LMX.reg[35],1,12,1)                          #Seting Mash_Reset to zero if system in is fractional mode

    elif newParams[2] >7 and newParams[1] != 0:

        LMX.reg[35] = modifyBM(LMX.reg[35],2,7,2)                          #Seting MASH modulator to second order if DEN > 7

        LMX.reg[35] = modifyBM(LMX.reg[35],1,12,1)                          #Seting Mash_Reset to zero if system in is fractional mode

  

    LMX.reg[42] = BMofNumber(newParams[1],16,16)                          #Setting MSBs of NUM
    
    #print("LMX.reg[43]=",LMX.reg[43])
    #print("newParams[1]=",hex(newParams[1]))


    LMX.reg[43] = BMofNumber(newParams[1],0,16)                           #Setting LSBs of NUM

    #print("LMX.reg[43]=",LMX.reg[43])
    #print("exiting early")
    #sys.exit()


    LMX.reg[38] = BMofNumber(newParams[2],16,16)                          #Setting MSBs of DEN

    LMX.reg[39] = BMofNumber(newParams[2],0,16)                           #Setting LSBs of DEN

    LMX.reg[36] = BMofNumber(newParams[0],0,15)                           #Setting INT

    INSTCAL_PLL = int(pow(2,32)*(newParams[1]/newParams[2]))              #calculating new INSCAL_PLL number

    LMX.reg[44] = BMofNumber(INSTCAL_PLL,16,16)                           #Setting INSTCAL MSB

    LMX.reg[45] = BMofNumber(INSTCAL_PLL,0,16)                            #Setting INSTCAL LSB





# end LMX2820setREGSforNewFreq

#

# ----------------------------------

#

#Write a single register to the LMX

def LMX2820WriteSingeRegister(spi, CS, LMX, address, NewValue):
    global SIMU

    debug_f = False

    b2 = address & 0xFF              # 1 byte address
    b1 = (NewValue & 0xFF00) >> 8    # MSB
    b0 = NewValue & 0xFF             # LSB

    icmd = [b2,b1,b0]                # array of type int
    bcmd = bytes(icmd)               # array of tkype bytes


    try:

      CSpin.switch_to_output(value=False)

    except Exception as eobj:

      if (SIMU):

        pass

      else:

        raise

    if (not SIMU):        # delay is for the SPI bus (not needed)
      pass # time.sleep(0.1)
    # endif


    if (debug_f):

      # print("address=", address,"byts2write=",[hex(x) for x in bcmd])
      #
      # format to match "TICSpro" saved output
      #
      s2 = f"{b2:02x}"
      s2 = s2.upper()
      s1 = f"{b1:02x}"
      s1 = s1.upper()
      s0 = f"{b0:02x}"
      s0 = s0.upper()
      print("R%d\t0x%s%s%s"%(address,
                           s2, 
                           s1,
                           s0) )
    # endif


    try:

      spi.write(bcmd)

    except Exception as eobj:

      if (SIMU):

        pass

      else:

        raise

    try: 

      CSpin.switch_to_output(value=True)

    except Exception as eobj:

      if (SIMU):

        pass

      else:

        raise




# end LMX2820WriteSingeRegister

#

# ----------------------------------

#

#Function to start up the LMX

#Initializes registers to 6 GHz and writes regs in the order described in the datasheet

def LMX2820StartUp(LMX,spi,cs):

    LMX2820InitRegs(LMX)                                        #Writting inital register values

    LMX2820setREGSforNewFreq(LMX,6e9)                           #Setting registers to 6 GHz

    LMX.reg[0] = modifyBM(LMX.reg[0],1,1,1)                     #Writting 1 to reset register

    LMX2820WriteSingeRegister(spi,cs,LMX,0,LMX.reg[0])                 #Tramsmitting reg 0

    LMX.reg[0] = modifyBM(LMX.reg[0],0,1,1)                     #Writting 0 to reset register

    LMX2820WriteSingeRegister(spi,cs,LMX,0,LMX.reg[0])                 #Tramsmitting reg 0

    for x in range(0,122):                                      #Transmitting initizlied registers from top (112) to bottom (0)

        LMX2820WriteSingeRegister(spi,cs,LMX,122-x,LMX.reg[122-x])

    time.sleep(10e-3)                                           #Waiting 10 ms per datasheet recommendation page 40

    LMX.reg[0] = modifyBM(LMX.reg[0],1,4,1)                     #Writting a 1 in FCal_En

    LMX2820WriteSingeRegister(spi,cs,LMX,0,LMX.reg[0])                 #Tramsmitting reg 0

# end LMX2820StartUp

#

# ----------------------------------

#

#Function to change the frequency fo the LMX

#Full frequency range allowed, 44.2 MHz to 22.6GHz

#Function writes necessary registers and sends new values to the LMX

#Function ends by writting a 1 to the FCAl to lock new frequency value

#

def LMX2820ChangeFreq(spi,cs,LMX,newFreq,newPwr=MAX_PWR):

    LMX2820setREGSforNewFreq(LMX,newFreq,newPwr)

    for x in range(0,80):                      # end value is less than

        LMX2820WriteSingeRegister(spi,cs,LMX,79-x,LMX.reg[79-x])

    time.sleep(10e-3)

    LMX.reg[0] = modifyBM(LMX.reg[0],1,4,1)                 #Writting a 1 in FCal_En

    LMX2820WriteSingeRegister(spi,cs,LMX,0,LMX.reg[0])

# end LMX2820ChangeFreq

#

#

# ----------------------------------

#     main

# ----------------------------------

#

if __name__ == '__main__':



  if (SIMU):

    print("\n*** FTDI SIMULATION !!! ***\n")





  #ctrl = SpiController(5)


 
  try:

    spi = busio.SPI(board.SCK,  # clock

                    board.MOSI, # mosi

                    board.MISO) # miso
  except Exception as eobj:
    if (SIMU):

      print("busio.SPI() Exception:",eobj)

    else:

      raise



  try:

    CSpin = DigitalInOut(board.D4)

    CSpin.switch_to_output(value=True)
  except Exception as eobj:
    if (SIMU):

      print("busio.SPI() Exception:",eobj)

    else:

      raise





  #print("init registers")

  LMX = LMX2820(200e6,  # RefFREQ                    # initialization

                    0,  # RefDoubler 0 -> bypasses

                    1,  # RefMultipler

                    1,  # PreRDiv

                    1)  # PostRDiv



  #print("load registers")

  LMX2820StartUp(LMX,spi,CSpin)

  time.sleep(1)
  LMX2820ChangeFreq(spi,CSpin,LMX,700e6)

  pwr_int = MAX_PWR
  err_f = False
  freq_f = False
  pwr_f = False
  while(True):        # outer loop
    #
    #                 # first of 2 sequential inner loops
    #
    while(True):
      print("set FREQUENCY or 'n' for 'no change', 'q' to quit> ",end="",flush=True)
      freq_str = sys.stdin.readline()
      freq_ascii = freq_str.strip()

      if (freq_ascii == 'q'):
        print("quit detected")
        sys.exit()
      # endif quit test

      nc_f = False
      if (freq_ascii == 'n'):
        if (freq_f):           # only allow change after initial setting
          print("no change")
          nc_f = True
          err_f = False
          break
        else:
          print("initial frequency never set")
        # end else
      # endif no change check
      #
      # still here?
      #
      try:
        freq_float = float(freq_ascii)
      except Exception as eobj:
        print("Exception:",eobj)
        print("try again")
        err_f = True
        freq_f = False
      else:
         err_f = False
         freq_f = True
         break
      # end else on try/except
    # end while

    if (not err_f):
      if (not nc_f):
        print("setting frequency:",freq_float)
        LMX2820ChangeFreq(spi,CSpin,LMX,freq_float,pwr_int)
      # endif not no change
      #
      #                 # second of 2 sequential inner loops
      #
      while (True):  
        print("set POWER, range 0..7 or 'n' for 'no change', 'q' to quit> ",
               end="",flush=True)
        pwr_str = sys.stdin.readline()
        pwr_ascii = pwr_str.strip()

        if (pwr_ascii == 'q'):
          print("quit detected")
          sys.exit()
        # endif quit test

        nc_f = False
        if (pwr_ascii == 'n'):
          print("no change")
          nc_f = True
          err_f = False
          break
        # endif no change check
        #
        # still here?
        #
        try:
          pwr_int = int(pwr_ascii)
        except Exception as eobj:
          print("Exception:",eobj)
          print("try again")
          err_f = True
          pwr_f = False
        else:
          if ((pwr_int >= 0) and (pwr_int <= 7)):
            err_f = False
            pwr_f = True
            
            break
          else:
            print("range error: %d, expect 0..7"%(pwr_int))
            err_f = True
            pwr_f = False
          # end else bad number
        # end else on try/except 
      # end while      
    
      if ((not err_f) and (not nc_f)):
        print("setting power:", pwr_int)
        NewValue = pwr_int<<1
        NewValue = NewValue | 0x0110
        LMX2820WriteSingeRegister(spi, CSpin, LMX, 79, NewValue)
      # endif

    # endif not error
    
  # end while 

#

# ----------------------------------

#     END OF FILE

# ----------------------------------

#

