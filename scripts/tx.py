#!python
# ----------------------------------------------------------------------------
# Copyright (c) 2017 Massachusetts Institute of Technology (MIT)
# All rights reserved.
#
# Distributed under the terms of the BSD 3-clause license.
#
# The full license is in the LICENSE file, distributed with this software.
# ----------------------------------------------------------------------------
"""Transmit waveforms with synchronized USRPs."""

import math
import multiprocessing
import os
import re
import sys
import time
from argparse import Action, ArgumentParser, Namespace, RawDescriptionHelpFormatter
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from itertools import chain, cycle, islice, repeat
from subprocess import call
from textwrap import TextWrapper, dedent, fill

import digital_rf as drf
import numpy as np

from gnuradio import blocks, gr, uhd


class Tx:
    """Transmit data in binary format from a single USRP."""

    def __init__(
        self,
        waveform_files=[None],
        mboards=[],
        subdevs=["A:0"],
        centerfreqs=[440e6],  # Deal with this
        lo_offsets=[0],
        lo_sources=[""],
        lo_exports=[None],
        dc_offsets=[None],
        iq_balances=[None],
        gains=[0],
        bandwidths=[0],
        antennas=[""],
        samplerate=1e6,
        cpu_format=None,
        dev_args=[],
        stream_args=[],
        tune_args=[],
        sync=True,
        sync_source="gpsdo",
        realtime=False,
        verbose=True,
        test_settings=True,
        logger=print,
        tevent=multiprocessing.Event(),
    ):
        """
        Parmaeters
        ----------
        waveform_files : list
            List of waveform file paths. Files should be binary in interleaved short complex format. default: [None]
        mboards :
            List of mboards. Default =[]
        subdevs
            List of subdevices, default:["A:0"],
        centerfreqs=,# Deal with this
            List of center frequencies. default [440e6]
        lo_offsets=[0],
        lo_sources=[""],
        lo_exports=[None],
        dc_offsets=[None],
        iq_balances=[None],
        gains=[0],
        bandwidths=[0],
        antennas=[""],
        samplerate=1e6,
        cpu_format=None,
        dev_args=[],
        stream_args=[],
        tune_args=[],
        sync=True,
        sync_source="gpsdo",
        realtime=False,
        verbose=True,
        test_settings=True,
        muxed_dual_channel=False,
        logger=print,"""
        options = locals()
        del options["self"]
        op = self._parse_options(**options)
        self.op = op

        if op.test_settings:
            # test usrp device settings, release device when done
            if op.verbose:
                op.logger("Initialization: testing device settings.")
            u = self._usrp_setup()
            del u

    @staticmethod
    def _parse_options(**kwargs):
        """Put all keyword options in a namespace and normalize them."""
        op = Namespace(**kwargs)

        op.cpu_format = "sc16"

        # determine mboard and subdev per channel, get number of channels
        op.mboards_bychan = []
        op.subdevs_bychan = []
        op.mboardnum_bychan = []
        mboards = op.mboards if op.mboards else ["default"]
        for mbnum, (mb, sd) in enumerate(zip(mboards, op.subdevs)):
            sds = sd.split()
            mbs = list(repeat(mb, len(sds)))
            mbnums = list(repeat(mbnum, len(sds)))
            op.mboards_bychan.extend(mbs)
            op.subdevs_bychan.extend(sds)
            op.mboardnum_bychan.extend(mbnums)
        op.nmboards = len(op.mboards) if len(op.mboards) > 0 else 1
        op.nchs = len(op.mboards_bychan)

        # repeat arguments as necessary
        op.waveform_files = list(islice(cycle(op.waveform_files), 0, op.nchs))
        op.subdevs = list(islice(cycle(op.subdevs), 0, op.nmboards))
        op.centerfreqs = list(islice(cycle(op.centerfreqs), 0, op.nchs))
        op.dc_offsets = list(islice(cycle(op.dc_offsets), 0, op.nchs))
        op.iq_balances = list(islice(cycle(op.iq_balances), 0, op.nchs))
        op.lo_offsets = list(islice(cycle(op.lo_offsets), 0, op.nchs))
        op.lo_sources = list(islice(cycle(op.lo_sources), 0, op.nchs))
        op.lo_exports = list(islice(cycle(op.lo_exports), 0, op.nchs))
        op.gains = list(islice(cycle(op.gains), 0, op.nchs))
        op.bandwidths = list(islice(cycle(op.bandwidths), 0, op.nchs))
        op.antennas = list(islice(cycle(op.antennas), 0, op.nchs))

        # create device_addr string to identify the requested device(s)
        op.mboard_strs = []
        for n, mb in enumerate(op.mboards):
            if re.match(r"[^0-9]+=.+", mb):
                idtype, mb = mb.split("=")
            elif re.match(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", mb):
                idtype = "addr"
            elif (
                re.match(r"usrp[123]", mb)
                or re.match(r"b2[01]0", mb)
                or re.match(r"x3[01]0", mb)
            ):
                idtype = "type"
            elif re.match(r"[0-9A-Fa-f]{1,}", mb):
                idtype = "serial"
            else:
                idtype = "name"
            if len(op.mboards) == 1:
                # do not use identifier numbering if only using one mainboard
                s = f"{idtype}={mb.strip()}"
            else:
                s = f"{idtype}{n}={mb.strip()}"
            op.mboard_strs.append(s)

        if op.verbose:
            opstr = (
                dedent(
                    """\
                Main boards: {mboard_strs}
                Subdevices: {subdevs}
                Frequency: {centerfreqs}
                LO frequency offset: {lo_offsets}
                LO source: {lo_sources}
                LO export: {lo_exports}
                DC offset: {dc_offsets}
                IQ balance: {iq_balances}
                Gain: {gains}
                Bandwidth: {bandwidths}
                Antenna: {antennas}
                Device arguments: {dev_args}
                Stream arguments: {stream_args}
                Tune arguments: {tune_args}
                Sample rate: {samplerate}
            """
                )
                .strip()
                .format(**op.__dict__)
            )
            op.logger(opstr)

        # check that subdevice specifications are unique per-mainboard
        for sd in op.subdevs:
            sds = sd.split()
            if len(set(sds)) != len(sds):
                errstr = (
                    'Invalid subdevice specification: "{0}". '
                    "Each subdevice specification for a given mainboard must "
                    "be unique."
                )
                raise ValueError(errstr.format(sd))

        return op

    def _usrp_setup(self):
        """Create, set up, and return USRP sink object."""
        op = self.op
        # create usrp sink block
        u = uhd.usrp_sink(
            device_addr=",".join(chain(op.mboard_strs, op.dev_args)),
            stream_args=uhd.stream_args(
                cpu_format=op.cpu_format,
                otw_format="sc16",
                channels=list(range(op.nchs)),
                args=",".join(op.stream_args),
            ),
        )

        # set clock and time source if synced
        if op.sync:
            try:
                u.set_clock_source(op.sync_source, uhd.ALL_MBOARDS)
                u.set_time_source(op.sync_source, uhd.ALL_MBOARDS)
            except RuntimeError:
                errstr = f"Unknown sync_source option: '{op.sync_source}'. Must be one of {u.get_clock_sources(0)}."
                raise ValueError(errstr)

        # check for ref lock
        mbnums_with_ref = [
            mb_num
            for mb_num in range(op.nmboards)
            if "ref_locked" in u.get_mboard_sensor_names(mb_num)
        ]
        if mbnums_with_ref and op.sync:
            if op.verbose:
                sys.stdout.write("Waiting for reference lock...")
                sys.stdout.flush()
            timeout = 0
            while not all(
                u.get_mboard_sensor("ref_locked", mb_num).to_bool()
                for mb_num in mbnums_with_ref
            ):
                if op.verbose:
                    sys.stdout.write(".")
                    sys.stdout.flush()
                time.sleep(1)
                timeout += 1
                if timeout > 30:
                    if op.verbose:
                        sys.stdout.write("failed\n")
                        sys.stdout.flush()
                    raise RuntimeError("Failed to lock to 10 MHz reference.")
            if op.verbose:
                sys.stdout.write("locked\n")
                sys.stdout.flush()

        # Check if GPSDO is locked to gps
        if op.sync_source == "gpsdo" and op.sync:
            if op.verbose:
                sys.stdout.write("Checking for gpsdo lock...")
                sys.stdout.flush()

            gps_lock_list = [
                u.get_mboard_sensor("gps_locked", mb_num).to_bool()
                for mb_num in mbnums_with_ref
            ]
            if all(gps_lock_list):
                if op.verbose:
                    sys.stdout.write("GPSDO locked.\n")
                    sys.stdout.flush()
        # set mainboard options
        for mb_num in range(op.nmboards):
            u.set_subdev_spec(op.subdevs[mb_num], mb_num)
        # set global options
        # sample rate
        u.set_samp_rate(float(op.samplerate))
        # read back actual value
        samplerate = u.get_samp_rate()
        # calculate longdouble precision sample rate
        # (integer division of clock rate)
        cr = u.get_clock_rate()
        srdec = int(round(cr / samplerate))
        samplerate_ld = np.longdouble(cr) / srdec
        op.samplerate = samplerate_ld
        sr_rat = Fraction(cr).limit_denominator() / srdec
        op.samplerate_num = sr_rat.numerator
        op.samplerate_den = sr_rat.denominator

        # set per-channel options
        # set command time so settings are synced
        COMMAND_DELAY = 0.2
        cmd_time = u.get_time_now() + uhd.time_spec(COMMAND_DELAY)
        u.set_command_time(cmd_time, uhd.ALL_MBOARDS)
        for ch_num in range(op.nchs):
            # local oscillator sharing settings
            lo_source = op.lo_sources[ch_num]
            if lo_source:
                try:
                    u.set_lo_source(lo_source, uhd.ALL_LOS, ch_num)
                except RuntimeError:
                    errstr = (
                        f"Unknown LO source option: '{lo_source}'. Must be one of {u.get_lo_sources(uhd.ALL_LOS, ch_num)},"
                        " or it may not be possible to set the LO source on"
                        " this daughterboard."
                    )
                    raise ValueError(errstr)
            lo_export = op.lo_exports[ch_num]
            if lo_export is not None:
                if not lo_source:
                    errstr = f"Channel {ch_num}: must set an LO source in order to set LO export."
                    raise ValueError(errstr)
                u.set_lo_export_enabled(lo_export, uhd.ALL_LOS, ch_num)
            # center frequency and tuning offset
            tune_res = u.set_center_freq(
                uhd.tune_request(
                    op.centerfreqs[ch_num],
                    op.lo_offsets[ch_num],
                    args=uhd.device_addr(",".join(op.tune_args)),
                ),
                ch_num,
            )
            # store actual values from tune result
            op.centerfreqs[ch_num] = tune_res.actual_rf_freq + tune_res.actual_dsp_freq
            op.lo_offsets[ch_num] = -tune_res.actual_dsp_freq
            # dc offset
            dc_offset = op.dc_offsets[ch_num]
            if dc_offset is not None:
                u.set_dc_offset(dc_offset, ch_num)
            # iq balance
            iq_balance = op.iq_balances[ch_num]
            if iq_balance is not None:
                u.set_iq_balance(iq_balance, ch_num)
            # gain
            u.set_gain(op.gains[ch_num], ch_num)
            # bandwidth
            bw = op.bandwidths[ch_num]
            if bw:
                u.set_bandwidth(bw, ch_num)
            # antenna
            ant = op.antennas[ch_num]
            if ant:
                try:
                    u.set_antenna(ant, ch_num)
                except RuntimeError:
                    errstr = f"Unknown RX antenna option: '{ant}'. Must be one of {u.get_antennas(ch_num)}."
                    raise ValueError(errstr)

        # commands are done, clear time
        u.clear_command_time(uhd.ALL_MBOARDS)
        time.sleep(COMMAND_DELAY)

        # read back actual channel settings
        for ch_num in range(op.nchs):
            if op.lo_sources[ch_num]:
                op.lo_sources[ch_num] = u.get_lo_source(uhd.ALL_LOS, ch_num)
            if op.lo_exports[ch_num] is not None:
                op.lo_exports[ch_num] = u.get_lo_export_enabled(uhd.ALL_LOS, ch_num)
            op.gains[ch_num] = u.get_gain(ch_num)
            op.bandwidths[ch_num] = u.get_bandwidth(chan=ch_num)
            op.antennas[ch_num] = u.get_antenna(chan=ch_num)

        if op.verbose:
            op.logger("Using the following devices:")
            chinfostrs = [
                "Motherboard: {mb_id} ({mb_addr}) | Daughterboard: {db_name}",
                "Subdev: {sub} | Antenna: {ant} | Gain: {gain} | Rate: {sr}",
                "Frequency: {freq:.3f} ({lo_off:+.3f}) | Bandwidth: {bw}",
            ]
            if any(op.lo_sources) or any(op.lo_exports):
                chinfostrs.append("LO source: {lo_source} | LO export: {lo_export}")
            chinfo = "\n".join(["  " + ln for ln in chinfostrs])
            for ch_num in range(op.nchs):
                header = f"---- {ch_num} "
                header += "-" * (78 - len(header))
                op.logger(header)
                usrpinfo = dict(u.get_usrp_info(chan=ch_num))
                info = {}
                info["mb_id"] = usrpinfo["mboard_id"]
                mba = op.mboards_bychan[ch_num]
                if mba == "default":
                    mba = usrpinfo["mboard_serial"]
                info["mb_addr"] = mba
                info["db_name"] = usrpinfo["tx_subdev_name"]
                info["sub"] = op.subdevs_bychan[ch_num]
                info["ant"] = op.antennas[ch_num]
                info["bw"] = op.bandwidths[ch_num]
                info["dc_offset"] = op.dc_offsets[ch_num]
                info["freq"] = op.centerfreqs[ch_num]
                info["gain"] = op.gains[ch_num]
                info["iq_balance"] = op.iq_balances[ch_num]
                info["lo_off"] = op.lo_offsets[ch_num]
                info["lo_source"] = op.lo_sources[ch_num]
                info["lo_export"] = op.lo_exports[ch_num]
                info["sr"] = op.samplerate
                op.logger(chinfo.format(**info))
                op.logger("-" * 78)

        return u

    def run(self, starttime=None, endtime=None, duration=None, period=10):
        op = self.op

        # window in seconds that we allow for setup time so that we don't
        # issue a start command that's in the past when the flowgraph starts
        SETUP_TIME = 10

        # print current time and NTP status
        if op.verbose and sys.platform.startswith("linux"):
            try:
                call(("timedatectl", "status"))
            except OSError:
                # no timedatectl command, ignore
                pass

        # parse time arguments
        st = drf.util.parse_identifier_to_time(starttime)
        if st is not None:
            # find next suitable start time by cycle repeat period
            now = datetime.now(tz=timezone.utc)
            soon = now + timedelta(seconds=SETUP_TIME)
            diff = max(soon - st, timedelta(0)).total_seconds()
            periods_until_next = (diff - 1) // period + 1
            st = st + timedelta(seconds=periods_until_next * period)

            if op.verbose:
                ststr = st.strftime("%a %b %d %H:%M:%S %Y")
                stts = (st - drf.util.epoch).total_seconds()
                print(f"Start time: {ststr} ({stts})")

        et = drf.util.parse_identifier_to_time(endtime, ref_datetime=st)
        if et is not None:
            if op.verbose:
                etstr = et.strftime("%a %b %d %H:%M:%S %Y")
                etts = (et - drf.util.epoch).total_seconds()
                print(f"End time: {etstr} ({etts})")

            if (
                et < (datetime.now(tz=timezone.utc) + timedelta(seconds=SETUP_TIME))
            ) or (st is not None and et <= st):
                raise ValueError("End time is before launch time!")

        if op.realtime:
            r = gr.enable_realtime_scheduling()

            if op.verbose:
                if r == gr.RT_OK:
                    print("Realtime scheduling enabled")
                else:
                    print("Note: failed to enable realtime scheduling")

        # wait for the start time if it is not past
        while (st is not None) and (
            (st - datetime.now(tz=timezone.utc)) > timedelta(seconds=SETUP_TIME)
        ):
            ttl = int((st - datetime.now(tz=timezone.utc)).total_seconds())
            if (ttl % 10) == 0:
                print(f"Standby {ttl} s remaining...")
                sys.stdout.flush()
            time.sleep(1)

        # get UHD USRP source
        u = self._usrp_setup()

        # set device time
        tt = time.time()
        if op.sync:
            # wait until time 0.2 to 0.5 past full second, then latch
            # we have to trust NTP to be 0.2 s accurate
            while tt - math.floor(tt) < 0.2 or tt - math.floor(tt) > 0.3:
                time.sleep(0.01)
                tt = time.time()
            if op.verbose:
                print("Latching at " + str(tt))
            # waits for the next pps to happen
            # (at time math.ceil(tt))
            # then sets the time for the subsequent pps
            # (at time math.ceil(tt) + 1.0)
            u.set_time_unknown_pps(uhd.time_spec(math.ceil(tt) + 1.0))
            # wait for time registers to be in known state
            time.sleep(math.ceil(tt) - tt + 1.0)
        else:
            u.set_time_now(uhd.time_spec(tt), uhd.ALL_MBOARDS)
            # wait for time registers to be in known state
            time.sleep(1)

        # set launch time
        # (at least 1 second out so USRP start time can be set properly and
        #  there is time to set up flowgraph)
        if st is not None:
            lt = st
        else:
            now = datetime.now(tz=timezone.utc)
            # launch on integer second by default for convenience  (ceil + 1)
            lt = now.replace(microsecond=0) + timedelta(seconds=2)
        ltts = (lt - drf.util.epoch).total_seconds()
        # adjust launch time forward so it falls on an exact sample since epoch
        lt_samples = np.ceil(ltts * op.samplerate)
        ltts = float(lt_samples / op.samplerate)
        lt = drf.util.sample_to_datetime(lt_samples, op.samplerate)
        if op.verbose:
            ltstr = lt.strftime("%a %b %d %H:%M:%S.%f %Y")
            print(f"Launch time: {ltstr} ({ltts!r})")
        # command launch time
        ct_td = lt - drf.util.epoch
        ct_secs = ct_td.total_seconds() // 1.0
        ct_frac = ct_td.microseconds / 1000000.0
        u.set_start_time(uhd.time_spec(ct_secs) + uhd.time_spec(ct_frac))

        # populate flowgraph one channel at a time
        fg = gr.top_block()
        for k in range(op.nchs):
            repeat = True
            offset = 0
            length = 0
            src_k = blocks.file_source(
                gr.sizeof_short * 2, op.waveform_files[k], repeat, offset, length
            )
            src_k.set_output_multiple(int(op.samplerate / 5))
            fg.connect(src_k, (u, k))

        # start the flowgraph once we are near the launch time
        # (start too soon and device buffers might not yet be flushed)
        # (start too late and device might not be able to start in time)
        while (lt - datetime.now(tz=timezone.utc)) > timedelta(seconds=1.2):
            time.sleep(0.1)
        fg.start()

        # wait until end time or until flowgraph stops
        if et is None and duration is not None:
            et = lt + timedelta(seconds=duration)
        try:
            if et is None:
                fg.wait()
            else:
                # sleep until end time nears
                while datetime.now(tz=timezone.utc) < et - timedelta(seconds=2):
                    time.sleep(1)
                # issue stream stop command at end time
                ct_td = et - drf.util.epoch
                ct_secs = ct_td.total_seconds() // 1.0
                ct_frac = ct_td.microseconds / 1000000.0
                u.set_command_time(
                    (uhd.time_spec(ct_secs) + uhd.time_spec(ct_frac)),
                    uhd.ALL_MBOARDS,
                )
                stop_enum = uhd.stream_cmd.STREAM_MODE_STOP_CONTINUOUS
                u.issue_stream_cmd(uhd.stream_cmd(stop_enum))
                u.clear_command_time(uhd.ALL_MBOARDS)
                # sleep until after end time
                time.sleep(2)
        except KeyboardInterrupt:
            # catch keyboard interrupt and simply exit
            pass
        fg.stop()
        # need to wait for the flowgraph to clean up, otherwise it won't exit
        fg.wait()
        print("done")
        sys.stdout.flush()


def evalint(s):
    """Evaluate string to an integer."""
    return int(eval(s, {}, {}))


def evalfloat(s):
    """Evaluate string to a float."""
    return float(eval(s, {}, {}))


def noneorstr(s):
    """Turn empty or 'none' string to None."""
    if s.lower() in ("", "none"):
        return None
    else:
        return s


def noneorbool(s):
    """Turn empty or 'none' string to None, all others to boolean."""
    if s.lower() in ("", "none"):
        return None
    elif s.lower() in ("true", "t", "yes", "y", "1"):
        return True
    else:
        return False


def noneorcomplex(s):
    """Turn empty or 'none' to None, else evaluate to complex."""
    if s.lower() in ("", "none"):
        return None
    else:
        return complex(eval(s, {}, {}))


class Extend(Action):
    """Action to split comma-separated arguments and add to a list."""

    def __init__(self, option_strings, dest, type=None, **kwargs):
        if type is not None:
            itemtype = type
        else:

            def itemtype(s):
                return s

        def split_string_and_cast(s):
            return [itemtype(a.strip()) for a in s.strip().split(",")]

        super().__init__(option_strings, dest, type=split_string_and_cast, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        cur_list = getattr(namespace, self.dest, [])
        if cur_list is None:
            cur_list = []
        cur_list.extend(values)
        setattr(namespace, self.dest, cur_list)


def _build_tx_parser(Parser, *args):
    """Creates the parser for the tx"""
    scriptname = os.path.basename(sys.argv[0])

    formatter = RawDescriptionHelpFormatter(scriptname)
    width = formatter._width

    title = "txcmd.py"
    copyright = "Copyright (c) 2017 Massachusetts Institute of Technology"
    shortdesc = "Transmit a waveform on a loop using synchronized USRPs."
    desc = "\n".join(
        (
            "*" * width,
            "*{0:^{1}}*".format(title, width - 2),
            "*{0:^{1}}*".format(copyright, width - 2),
            "*{0:^{1}}*".format("", width - 2),
            "*{0:^{1}}*".format(shortdesc, width - 2),
            "*" * width,
        )
    )

    usage = (
        "%(prog)s [-m MBOARD] [-d SUBDEV] [-c CH] [-y ANT] [-f FREQ]"
        " [-F OFFSET] \\\n"
        "{0:8}[-g GAIN] [-b BANDWIDTH] [-r RATE] [options]"
        " FILE\n".format("")
    )

    epi_pars = [
        """\
        Arguments in the "mainboard" and "channel" groups accept multiple
        values, allowing multiple mainboards and channels to be specified.
        Multiple arguments can be provided by repeating the argument flag, by
        passing a comma-separated list of values, or both. Within each argument
        group, parameters will be grouped in the order in which they are given
        to form the complete set of parameters for each mainboard/channel. For
        any argument with fewer values given than the number of
        mainboards/channels, its values will be extended by repeatedly cycling
        through the values given up to the needed number.
        """,
        """\
        Arguments in other groups apply to all mainboards/channels (including
        the sample rate).
        """,
        """\
        Example usage:
        """,
    ]
    epi_pars = [fill(dedent(s), width) for s in epi_pars]

    egtw = TextWrapper(
        width=(width - 2),
        break_long_words=False,
        break_on_hyphens=False,
        subsequent_indent=" " * (len(scriptname) + 1),
    )
    egs = [
        """\
        {0} -m 192.168.10.2 -d "A:0" -f 440e6 -F 12.5e6 -G 0.25 -g 0 -r 1e6
        code.bin
        """
    ]
    egs = [" \\\n".join(egtw.wrap(dedent(s.format(scriptname)))) for s in egs]
    epi = "\n" + "\n\n".join(epi_pars + egs) + "\n"

    # parse options
    parser = Parser(
        description=desc,
        usage=usage,
        epilog=epi,
        formatter_class=RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-q",
        "--quiet",
        dest="verbose",
        action="store_false",
        help="""Reduce text output to the screen. (default: False)""",
    )
    parser.add_argument(
        "--notest",
        dest="test_settings",
        action="store_false",
        help="""Do not test USRP settings until experiment start.
                (default: False)""",
    )

    wavgroup = parser.add_argument_group(title="waveform")
    wavgroup.add_argument(
        "waveform_files",
        nargs="+",
        help="""Binary files of interleaved short complex dtype giving a waveform.""",
    )

    mbgroup = parser.add_argument_group(title="mainboard")
    mbgroup.add_argument(
        "-m",
        "--mainboard",
        dest="mboards",
        action=Extend,
        help="""Mainboard address. (default: first device found)""",
    )
    mbgroup.add_argument(
        "-d",
        "--subdevice",
        dest="subdevs",
        action=Extend,
        help="""USRP subdevice string. (default: "A:0")""",
    )

    chgroup = parser.add_argument_group(title="channel")
    chgroup.add_argument(
        "-f",
        "--centerfreq",
        dest="centerfreqs",
        action=Extend,
        type=float,
        help="""Center frequency in Hz. (default: 440e6)""",
    )
    chgroup.add_argument(
        "-F",
        "--lo_offset",
        dest="lo_offsets",
        action=Extend,
        type=float,
        help="""Frontend tuner offset from center frequency, in Hz.
                (default: 0)""",
    )
    chgroup.add_argument(
        "--lo_source",
        dest="lo_sources",
        action=Extend,
        type=noneorstr,
        help="""Local oscillator source. Typically 'None'/'' (do not set),
                'internal' (e.g. LO1 for CH1, LO2 for CH2),
                'companion' (e.g. LO2 for CH1, LO1 for CH2), or
                'external' (neighboring board via connector).
                (default: '')""",
    )
    chgroup.add_argument(
        "--lo_export",
        dest="lo_exports",
        action=Extend,
        type=noneorbool,
        help="""Whether to export the LO's source to the external connector.
                Can be 'None'/'' to skip the channel, otherwise it can be
                'True' or 'False' provided the LO source is set.
                (default: None)""",
    )
    chgroup.add_argument(
        "--dc_offset",
        dest="dc_offsets",
        action=Extend,
        type=noneorcomplex,
        help="""DC offset correction to use. Can be 'None'/'' to keep device
                default or a complex value (e.g. "1+1j"). (default: 0)""",
    )
    chgroup.add_argument(
        "--iq_balance",
        dest="iq_balances",
        action=Extend,
        type=noneorcomplex,
        help="""IQ balance correction to use. Can be 'None'/'' to keep device
                default or a complex value (e.g. "1+1j"). (default: 0)""",
    )
    chgroup.add_argument(
        "-g",
        "--gain",
        dest="gains",
        action=Extend,
        type=float,
        help="""USRP frontend gain in dB. (default: 0)""",
    )
    chgroup.add_argument(
        "-b",
        "--bandwidth",
        dest="bandwidths",
        action=Extend,
        type=float,
        help="""Frontend bandwidth in Hz. (default: 0 == frontend default)""",
    )
    chgroup.add_argument(
        "-y",
        "--antenna",
        dest="antennas",
        action=Extend,
        type=noneorstr,
        help="""Name of antenna to select on the frontend.
                (default: 'None' == frontend default))""",
    )

    txgroup = parser.add_argument_group(title="transmitter")
    txgroup.add_argument(
        "-r",
        "--samplerate",
        dest="samplerate",
        type=evalfloat,
        help="""Sample rate in Hz. (default: waveform default or 1e6)""",
    )
    txgroup.add_argument(
        "-A",
        "--devargs",
        dest="dev_args",
        action=Extend,
        help="""Device arguments, e.g. "master_clock_rate=40e6,num_send_frames=512,send_buff_size=1000000".
                (default: '')""",
    )
    txgroup.add_argument(
        "-a",
        "--streamargs",
        dest="stream_args",
        action=Extend,
        help="""Stream arguments, e.g. "fullscale=1.0".
                (default: '')""",
    )
    txgroup.add_argument(
        "-T",
        "--tuneargs",
        dest="tune_args",
        action=Extend,
        help="""Tune request arguments, e.g. "mode_n=integer,int_n_step=100e3".
                (default: '')""",
    )
    txgroup.add_argument(
        "--sync_source",
        dest="sync_source",
        help="""Clock and time source for all mainboards.
                (default: 'external')""",
    )
    txgroup.add_argument(
        "--nosync",
        dest="sync",
        action="store_false",
        help="""No syncing with external clock. (default: False)""",
    )
    txgroup.add_argument(
        "--realtime",
        dest="realtime",
        action="store_true",
        help="""Enable realtime scheduling if possible.
                (default: %(default)s)""",
    )

    timegroup = parser.add_argument_group(title="time")
    timegroup.add_argument(
        "-s",
        "--starttime",
        dest="starttime",
        help="""Start time of the experiment as datetime (if in ISO8601 format:
                2016-01-01T15:24:00Z) or Unix time (if float/int).
                (default: start ASAP)""",
    )
    timegroup.add_argument(
        "-e",
        "--endtime",
        dest="endtime",
        help="""End time of the experiment as datetime (if in ISO8601 format:
                2016-01-01T16:24:00Z) or Unix time (if float/int).
                (default: wait for Ctrl-C)""",
    )
    timegroup.add_argument(
        "-l",
        "--duration",
        dest="duration",
        type=evalint,
        help="""Duration of experiment in seconds. When endtime is not given,
                end this long after start time. (default: wait for Ctrl-C)""",
    )
    timegroup.add_argument(
        "-p",
        "--cycle-length",
        dest="period",
        type=int,
        help="""Repeat time of experiment cycle. Align to start of next cycle
                if start time has passed. (default: 10)""",
    )

    return parser


def main(op):
    # remove redundant arguments in dev_args, stream_args, tune_args
    if op.dev_args is not None:
        try:
            dev_args_dict = dict([a.split("=") for a in op.dev_args])
        except ValueError:
            raise ValueError("Device arguments must be {KEY}={VALUE} pairs.")
        op.dev_args = [f"{k}={v}" for k, v in dev_args_dict.items()]
    if op.stream_args is not None:
        try:
            stream_args_dict = dict([a.split("=") for a in op.stream_args])
        except ValueError:
            raise ValueError("Stream arguments must be {KEY}={VALUE} pairs.")
        op.stream_args = [f"{k}={v}" for k, v in stream_args_dict.items()]
    if op.tune_args is not None:
        try:
            tune_args_dict = dict([a.split("=") for a in op.tune_args])
        except ValueError:
            raise ValueError("Tune request arguments must be {KEY}={VALUE} pairs.")
        op.tune_args = [f"{k}={v}" for k, v in tune_args_dict.items()]

    # ignore test_settings option if no starttime is set (starting right now)
    if op.starttime is None:
        op.test_settings = False

    options = {k: v for k, v in op._get_kwargs() if v is not None}

    runopts = {
        k: options.pop(k)
        for k in list(options.keys())
        if k in ("starttime", "endtime", "duration", "period")
    }

    tx = Tx(**options)
    tx.run(**runopts)


if __name__ == "__main__":
    parser = _build_tx_parser(ArgumentParser)
    args = parser.parse_args()
    main(args)
