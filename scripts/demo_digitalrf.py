#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tutorial: reading 'digital_rf' data recorded on MEP.

Run 'conda activate base' on MEP before use.
"""

# ==== Imports ==== #
from datetime import datetime, timezone
from pathlib import Path

import digital_rf
import matplotlib.pyplot as plt
import numpy as np

# ==== User inputs ==== #
capture_name = "livestream"           # name of the capture folder
sample_rate_folder_name = "sr32MHz"   # sample rate folder
channel = "chC"                       # channel to read

# ==== Open the reader ==== #
rootdir = Path("/data/captures") / capture_name / sample_rate_folder_name
dro = digital_rf.DigitalRFReader(str(rootdir))
print("Channels in this capture:", dro.get_channels())

# Sample rate from channel properties
props = dro.get_properties(channel)
samp_rx = props["sample_rate_numerator"] / props["sample_rate_denominator"]

# ==== Metadata ==== #
# dict_meta contains things like center frequency, gain, etc.
# print(dict_meta.keys()) to see everything that was recorded.
dmd = dro.get_digital_metadata(channel)
md_start, md_end = dmd.get_bounds()
dict_meta = dmd.read_flatdict(md_start, md_end)

# ==== What data is available? ==== #
# get_continuous_blocks returns a dict of {start_index: n_samples} for
# each contiguous chunk of data. It skips gaps between blocks.
blocks = dro.get_continuous_blocks(*dro.get_bounds(channel), channel)

for block_start, block_len in blocks.items():
    t = datetime.fromtimestamp(block_start / samp_rx, tz=timezone.utc)
    print(f"  Block at {t} UTC, {block_len / samp_rx:.1f} s ({block_len} samples)")

# Use the first block
first_block_start = min(blocks.keys())

# ==== Read IQ data ==== #
# The start of the first block contains fill samples at int16 min (-32768)
# until the recorder receives a real sample. Adjust skip_seconds as needed.
skip_seconds     = 5
duration_seconds = 0.001

read_start       = first_block_start + int(skip_seconds * samp_rx)
duration_samples = int(duration_seconds * samp_rx)

channel_iq = dro.read_vector(read_start, duration_samples, channel)
# channel_iq is a numpy array of complex64 IQ samples

# Time axis in seconds relative to read_start
t      = np.arange(duration_samples) / samp_rx
t0_utc = datetime.fromtimestamp(read_start / samp_rx, tz=timezone.utc)

# ==== Plot IQ ==== #
fig, ax = plt.subplots()
ax.plot(t * 1e6, channel_iq.real, linewidth=0.5, label="I")
ax.plot(t * 1e6, channel_iq.imag, linewidth=0.5, label="Q")
ax.set_title(f"IQ - {channel} - {t0_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
ax.set_xlabel("Time (us)  # microseconds")
ax.set_ylabel("Amplitude (counts)")
ax.legend()

# ==== Constellation (I vs Q) ==== #
fig, ax = plt.subplots()
ax.scatter(channel_iq.real, channel_iq.imag, s=1, alpha=0.5)
ax.set_title(f"Constellation - {channel} - {t0_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
ax.set_xlabel("I (counts)")
ax.set_ylabel("Q (counts)")
ax.set_aspect("equal")

# ==== Spectrogram ==== #
# interpolation='none' disables any smoothing so bins are shown as-is.
fig, ax = plt.subplots(figsize=(10, 6))
Pxx, freqs, bins, im = ax.specgram(
    channel_iq, NFFT=1024, Fs=samp_rx, noverlap=512, scale="dB"
)
im.set_interpolation('none')
ax.set_title(f"Spectrogram - {channel} - {t0_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
ax.set_xlabel("Time (s)")
ax.set_ylabel("Frequency (Hz)")
fig.colorbar(im, ax=ax, label="Power (dB)")
fig.tight_layout()

plt.show()