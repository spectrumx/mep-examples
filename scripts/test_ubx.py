import socket

DEV = "/dev/ttyGNSS1"
SOCK = "/run/gpsd.sock"

def nmea(sentence_no_dollar_no_checksum: str) -> str:
    # example input: "PMITR?"
    csum = 0
    for ch in sentence_no_dollar_no_checksum:
        csum ^= ord(ch)
    return f"${sentence_no_dollar_no_checksum}*{csum:02X}\r\n"

def gpsd_inject_ascii(dev: str, payload: str):
    # payload is the exact bytes you want written to the device (string here = ASCII)
    cmd = f"!{dev}={payload}\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.sendall(cmd.encode("ascii"))
    s.close()
    print("sent:", repr(cmd))

payload = nmea("PMITR?")
gpsd_inject_ascii(DEV, payload)