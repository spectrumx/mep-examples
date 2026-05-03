# MEP Scope GUI Plan

## Summary

Create a standalone Tkinter GUI at `scripts/mep_scope.py` that reads the live DigitalRF ringbuffer directly and renders oscilloscope-style time-domain traces. The GUI will not import or use `MEPBus`, MQTT command publishers, RFSoC controls, or Xilinx tools.

The existing SPEC tab in `scripts/mep_gui.py` does not process raw DigitalRF data. It listens to MQTT spectrum messages on `radiohound/clients/data/#`, decodes base64 little-endian `float32` spectrum bins, converts them to dB, and plots those bins as a live FFT and waterfall. The new Scope GUI should instead follow the repo's DigitalRF reader precedent in `experiments/calculate_noise_figure.py`, which uses `DigitalRFReader`, `get_bounds()`, `get_continuous_blocks()`, and `read_vector()`.

## Key Changes

- Add `scripts/mep_scope.py` using Tkinter/ttk conventions similar to `scripts/mep_gui.py`.
- Default DigitalRF layout:
  - root: `/data/ringbuffer`
  - sample-rate directory: `sr{sample_rate_mhz}MHz`
  - channel: `ch{channel}`
  - reader top-level directory: `/data/ringbuffer/sr{sample_rate_mhz}MHz`
  - DigitalRF channel name: `ch{channel}`
- Support live channel paths like `/data/ringbuffer/sr10MHz/chB/2025-07-25T17-00-00/rf@1753463035.000.h5` without requiring the operator to select the timestamp subdirectory.
- Provide controls for ringbuffer root path, sample rate MHz, channel, window length, refresh interval, Run/Pause, and autoscale/manual Y range.
- Make sample rate MHz a readonly dropdown populated from the live ringbuffer contents: scan the selected root path for directories matching `sr{N}MHz`, sort them numerically, and use those `N` values as the available sample rates.
- Render I and Q as the primary time-domain traces versus time.
- Show status for current bounds, sample index range read, samples displayed, empty reads, and last error.

## Implementation Details

- Use a background reader thread so DigitalRF I/O never blocks Tk.
- Initialize the sample-rate control as `tk.StringVar(value="10")` with a `ttk.Combobox(..., state="readonly")`; refresh its values by inspecting `/data/ringbuffer` or the operator-selected root path.
- If no `sr{N}MHz` directories are found, leave the dropdown empty or retain the current value, show a status message that no ringbuffer sample-rate directories were found, and avoid falling back to recorder config options.
- On each refresh:
  - create or reuse `DigitalRFReader(top_level_dir)`
  - call `get_bounds(channel)`
  - read the newest complete window ending slightly behind the latest bound to avoid partially written files
  - use `get_continuous_blocks()` to avoid gaps
  - use `read_vector()` for the latest valid block/window
  - push only the latest decoded trace snapshot to the GUI thread through a queue
- Convert sample arrays to NumPy if available; otherwise handle array-like DigitalRF output directly.
- Plot on a Tk `Canvas`, matching the lightweight rendering approach used by the SPEC tab.
- Treat complex samples as:
  - I = `real(iq)`
  - Q = `imag(iq)`
- Handle empty or missing data gracefully with a visible paused/error status instead of crashing.

## Test Plan

- Static check: `python3 -m py_compile scripts/mep_scope.py`.
- Offline smoke checks:
  - validate path construction for `/data/ringbuffer/sr10MHz/chB`
  - validate trace downsampling and canvas coordinate mapping with synthetic complex data
- Live operator validation on a machine with DigitalRF:
  - run `python3 scripts/mep_scope.py --root /data/ringbuffer --sample-rate-mhz 10 --channel B`
  - confirm it reads newest data from `/data/ringbuffer/sr10MHz/chB/...`
  - confirm I and Q update continuously without MQTT connected
- Do not run Vivado, XSIM, XVLOG, XELAB, synthesis, implementation, block-design validation, project builds, or any other Xilinx tools in this environment.

## Assumptions

- `digital_rf` is installed on the target machine where the GUI runs.
- The live recorder writes a valid DigitalRF channel under `/data/ringbuffer/sr{N}MHz/ch{X}`.
- The first version only displays I and Q traces; magnitude can be added later as an optional overlay.
