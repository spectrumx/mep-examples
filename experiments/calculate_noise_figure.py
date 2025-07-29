#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Author: 
    John Marino (john.marino@colorado.edu)
Acknowledgments: 
    Alisa Yurevich
Date: 
    28 Jul 2025 (refactor), 17 Jul 2025 (original)
Purpose: 
    Function(s) to compute frequency-dependent Noise Figure 
     via Y-factor calibration method for data taken with SpectrumX MEPs 
References:
     - https://www.keysight.com/zz/en/assets/7018-06829/application-notes/5952-3706.pdf
     - Frank H. Sanders: NTIA Talk 10: https://www.youtube.com/watch?v=miwFe37PWjg 
"""

# ===== IMPORTS ===== #
# Standard Libraries
import os
import glob
from pathlib import Path
# Anaconda Distribution
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.signal import spectrogram
# External (PyPI)
import h5py
import yaml
from digital_rf import DigitalRFReader


# ===== NOISE DIODE ===== #
class NoiseDiode:
    """Represents a calibrated noise diode with ENR lookup."""

    def __init__(self, model, serial, enr_table):
        self.model = model
        self.serial = serial
        self.enr_table = dict(sorted(enr_table.items()))

    def enr_db(self, freq_ghz):
        freqs, vals = zip(*self.enr_table.items())
        return np.interp(freq_ghz, freqs, vals)

    def compute_noisefigure(self, P_on, P_off, freq_ghz):
        """Compute Noise Figure at a given frequency from ON/OFF power."""
        enr_db = self.enr_db(freq_ghz)
        enr_lin = 10**(enr_db / 10)
        Y = P_on / P_off
        NF_lin = enr_lin / (Y - 1)
        NF_dB = 10 * np.log10(NF_lin) if NF_lin is not None and NF_lin > 0 else np.nan
        return {
            'frequency_GHz': freq_ghz,
            'ENR_dB': enr_db,
            'Y': Y,
            'NF_dB': NF_dB,
            'P_on': P_on,
            'P_off': P_off,
            #'diode_model': self.model, # good to know, but redundant here
            #'diode_serial': self.serial, # good to know, but redundant here
        }

    def plot_enr(self, ax=None, dir_save=None):
        # Get table
        freqs, vals = zip(*self.enr_table.items())
        
        # Use supplied ax (for overplotting), or make a new fig, ax
        if ax is None:
            fig, ax = plt.subplots(figsize=(8, 4))
        else:
            fig = ax.get_figure()
    
        # Plot
        ax.plot(freqs, vals, "o-", label=f"{self.model} (SN: {self.serial})")
        ax.set(xlabel="Frequency (GHz)", ylabel="ENR (dB)")
        ax.grid(True)
        ax.legend()
    
        # Save plot
        if dir_save is not None:
            Path(dir_save).mkdir(parents=True, exist_ok=True)
            save_path = Path(dir_save) / f"enr_curve_{self.model}_{self.serial}.png"
            fig.savefig(save_path, dpi=300)
            print(f"... [NoiseDiode] Saved ENR plot to {save_path}")
    
        return fig, ax

    def save_yaml(self, path):
        data = {
            "model": self.model,
            "serial": self.serial,
            "enr_table": self.enr_table
        }
        with open(path, "w") as f:
            yaml.dump(data, f, sort_keys=False)
        print(f"... [NoiseDiode] Saved YAML to {path}")

    @classmethod
    def load_yaml(cls, path):
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return cls(data["model"], data["serial"], data["enr_table"])


# ===== FILE CHECKER ===== #
class FileChecker:
    """Handles collection and visualization of file availability."""
    @staticmethod
    def collect_valid_files(root_dir, file_pattern):
        matched_files = []
        for dirpath, dirnames, filenames in os.walk(root_dir):
            if 'metadata' in os.path.basename(dirpath).lower():
                continue
            if os.path.abspath(dirpath) == os.path.abspath(root_dir):
                continue
            matched_files.extend(glob.glob(os.path.join(dirpath, file_pattern)))
        return sorted(matched_files)

    @staticmethod
    def find_missing_files(files):
        file_numbers = []
        for f in files:
            try:
                number = int(f.split('@')[1].split('.')[0])
                file_numbers.append(number)
            except (IndexError, ValueError):
                continue
        if not file_numbers:
            return [], set(), None, None
        file_numbers = sorted(file_numbers)
        min_num, max_num = min(file_numbers), max(file_numbers)
        missing = set(range(min_num, max_num + 1)) - set(file_numbers)
        return file_numbers, missing, min_num, max_num

    @staticmethod
    def plot_available_data(files, title='Data Availability', save_filename=None):
        file_numbers, missing_numbers, min_num, max_num = FileChecker.find_missing_files(files)

        fig, ax = plt.subplots(figsize=(10, 2.5))
        ax.scatter(file_numbers, [1] * len(file_numbers), color='blue', label='Present Data', zorder=5)

        for missing in missing_numbers:
            ax.axvspan(missing - 0.5, missing + 0.5, color='lightgray', alpha=0.7)

        ax.set_xlabel('File Number')
        ax.set_yticks([])
        ax.set_title(title)
        ax.legend(loc='upper right', labels=['Present Data', 'Missing Data'])
        plt.tight_layout()

        if save_filename:
            fig.savefig(save_filename)
            print(f"... saved: {save_filename}")


# ===== METADATA INDEX ===== #
class MetadataIndex:
    """Extracts and caches metadata from HDF5 files, with error reporting."""
    @staticmethod
    def extract_metadata_from_h5(metadata_dir, output_file=None, error_file=None):
        records = []
        error_records = []

        print(f"☐ Extracting metadata from HDF5 files at {metadata_dir}")
        h5_files = sorted(glob.glob(os.path.join(metadata_dir, '**/metadata@*.h5'), recursive=True))

        for path in h5_files:
            try:
                with h5py.File(path, 'r') as f:
                    sample_keys = list(f.keys())
                    if not sample_keys:
                        msg = "No sample keys found in file"
                        print(f"... Failed to process {path}: {msg}")
                        error_records.append({"file": path, "error": msg})
                        continue

                    sample_key = sample_keys[0]
                    data = f[sample_key]
                    record = {'file': path, 'sample_index': int(sample_key)}

                    # Flatten datasets
                    for key in data.keys():
                        value = data[key]
                        if isinstance(value, h5py.Group):
                            for subkey in value.keys():
                                try:
                                    subval = value[subkey][()]
                                    if isinstance(subval, np.ndarray) and subval.shape == ():
                                        subval = subval.item()
                                    elif isinstance(subval, bytes):
                                        subval = subval.decode('utf-8')
                                    record[f"{key}/{subkey}"] = subval
                                except Exception as e:
                                    record[f"{key}/{subkey}"] = np.nan
                                    error_records.append({"file": path, "error": f"{key}/{subkey}: {e}"})
                        else:
                            try:
                                val = value[()]
                                if isinstance(val, np.ndarray) and val.shape == ():
                                    val = val.item()
                                elif isinstance(val, bytes):
                                    val = val.decode('utf-8')
                                record[key] = val
                            except Exception as e:
                                record[key] = np.nan
                                error_records.append({"file": path, "error": f"{key}: {e}"})

                    # Flatten attributes
                    for attr_key in data.attrs.keys():
                        try:
                            attr_val = data.attrs[attr_key]
                            if isinstance(attr_val, bytes):
                                attr_val = attr_val.decode('utf-8')
                            elif isinstance(attr_val, np.ndarray) and attr_val.shape == ():
                                attr_val = attr_val.item()
                            record[f"attr/{attr_key}"] = attr_val
                        except Exception as e:
                            record[f"attr/{attr_key}"] = np.nan
                            error_records.append({"file": path, "error": f"attr/{attr_key}: {e}"})

                    records.append(record)

            except Exception as e:
                print(f"... Failed to process {path}: {e}")
                error_records.append({"file": path, "error": str(e)})

        df_metadata = pd.DataFrame.from_records(records)
        if 'center_frequencies' in df_metadata.columns:
            df_metadata['center_frequency'] = df_metadata['center_frequencies'].apply(
                lambda x: x[0] if isinstance(x, (list, np.ndarray)) and len(x) > 0 else np.nan
            )
            df_metadata = df_metadata.drop(columns=['center_frequencies'])
        else:
            df_metadata['center_frequency'] = np.nan

        df_metadata = df_metadata.sort_values('sample_index').reset_index(drop=True)

        if output_file:
            df_metadata.to_csv(output_file, index=False)
            print(f"☐ Cached metadata to {output_file}")

        df_errors = pd.DataFrame(error_records)
        if error_file:
            df_errors.to_csv(error_file, index=False)
            print(f"... Saved metadata errors to {error_file}")

        return df_metadata, df_errors


# ===== IQ ANALYZER ===== #
class IQAnalyzer:
    """Handles IQ power statistics and spectrogram plotting."""

    @staticmethod
    def compute_power_stats(iq, fs=None, method='fft_bandpass', f_notch=None):
        # If all values are NaN, bail out early
        if np.all(np.isnan(iq)) or len(iq) == 0:
            return (np.nan,) * 4

        if method == 'iq_rms':
            inst_power = np.abs(iq) ** 2
        elif method == 'fft_bandpass':
            if fs is None:
                raise ValueError("Sample rate fs must be provided for FFT-based power estimation.")
            N = len(iq)
            window = np.hanning(N)
            iq_win = np.where(np.isnan(iq), 0, iq) * window  # replace NaNs with 0 for FFT

            fft_vals = np.fft.fftshift(np.fft.fft(iq_win, n=N))
            freqs = np.fft.fftshift(np.fft.fftfreq(N, d=1/fs))
            power_spectrum = np.abs(fft_vals) ** 2 / np.sum(window**2)

            mask = np.ones_like(power_spectrum, dtype=bool)
            if f_notch is not None:
                mask &= (np.abs(freqs) > f_notch)
            inst_power = power_spectrum[mask]
        else:
            raise ValueError(f"Unknown method: {method}")

        # Use nan-safe reducers
        return (
            np.nanmean(inst_power),
            np.nanstd(inst_power),
            np.nanmin(inst_power),
            np.nanmax(inst_power)
        )

    @staticmethod
    def plot_waterfall(iq_data, fs, fc=0.0, nperseg=4096, noverlap=1024,
                       cmap='viridis', save_file=None):
        """
        Plot a 2-sided spectrogram and return fig, ax.
        """
        f, t, Sxx = spectrogram(
            iq_data,
            fs=fs,
            window='hann',
            nperseg=nperseg,
            noverlap=noverlap,
            nfft=nperseg,
            return_onesided=False,
            mode='psd',
            scaling='density'
        )
    
        sort_idx = np.argsort(f)
        f = f[sort_idx]
        Sxx = Sxx[sort_idx, :]
    
        Sxx_dB = 10 * np.log10(np.abs(Sxx) + 1e-12)
        f_shifted = f + fc
    
        fig, ax = plt.subplots(figsize=(10, 6))
        cax = ax.imshow(
            Sxx_dB,
            extent=[t[0], t[-1], f_shifted[0]/1e6, f_shifted[-1]/1e6],
            aspect='auto',
            origin='lower',
            cmap=cmap,
            interpolation=None
        )
        fig.colorbar(cax, ax=ax, label='Power (dB)')
        ax.set_xlabel('Time (s)')
        ax.set_ylabel('Frequency (MHz)')
        ax.set_title(f'Spectrogram PSD (fc = {fc/1e6:.2f} MHz)')
        fig.tight_layout()
    
        if save_file:
            fig.savefig(save_file, dpi=300)
            print(f"... Saved spectrogram to: {save_file}")
    
        return fig, ax


# ===== YFACTOR PIPELINE ===== #
class YFactorPipeline:
    def __init__(self, dir_dataset, dir_template, diode, channel, dir_output=None):
        self.dir_dataset = Path(dir_dataset).resolve()
        self.dir_template = dir_template
        self.diode = diode
        self.channel = channel

        base_name = self.dir_dataset.name
        root_output = Path(dir_output) if dir_output else Path.cwd()
        self.dir_output = root_output / f"outputs_{base_name}"
        self.dir_output.mkdir(parents=True, exist_ok=True)

        self.base_name = base_name
        self.df_metadata = {}
        self.df_metadata_errors = {}
        self.df_stats = {}
        self.df_noisefigure = None

        # Save the diode configuration as YAML automatically
        diode_path = self.dir_output / f"diode_{diode.model}_{diode.serial}.yaml"
        self.diode.save_yaml(diode_path)
        self.diode_yaml_file = diode_path.name
        
    def _resolve_path(self, state: str) -> Path:
        """Resolve a full path from the template and state ('on' or 'off')."""
        path = Path(self.dir_template.format(
            dir_dataset=self.dir_dataset,
            on_or_off=state,
            channel=self.channel
        )).resolve()
        return path

    def check_digitalrf_files(self, state, save_plot=True):
        directory = self._resolve_path(state)
        list_files = FileChecker.collect_valid_files(str(directory), "rf@*.h5")
    
        # Build descriptive title
        rel_path = directory.relative_to(self.dir_dataset)
        title = f"Data Availability\n{self.base_name}\n{rel_path}"
    
        rel_str = str(rel_path).replace(os.sep, "_")
        save_name = f"data_availability_{self.base_name}_{rel_str}.png"
        save_path = self.dir_output / save_name
    
        if save_plot:
            FileChecker.plot_available_data(list_files, title=title, save_filename=str(save_path))
    
        print(f"[FileCheck] {len(list_files)} files checked for {state.upper()}")
        return list_files

    
    def build_metadata_index(self, state, cache_file=None):
        metadata_dir = self._resolve_path(state) / "metadata"
        cache_file = cache_file or f"metadata_{state}_{self.base_name}.csv"
        error_file = f"metadata_errors_{state}_{self.base_name}.csv"
        cache_path = self.dir_output / cache_file
        error_path = self.dir_output / error_file
    
        self.df_metadata[state], self.df_metadata_errors[state] = MetadataIndex.extract_metadata_from_h5(
            str(metadata_dir), str(cache_path), str(error_path)
        )
        return self.df_metadata[state], self.df_metadata_errors[state]



    def compute_stats(self, on_or_off, cache_file=None, save_spectrograms=True):
        # Resolve the full channel path
        path_str = self.dir_template.format(
            dir_dataset=self.dir_dataset,
            on_or_off=on_or_off,
            channel=self.channel
        )
        channel_path = Path(path_str).resolve()
    
        # DigitalRFReader needs the parent directory (not the channel itself)
        drf_reader = DigitalRFReader(str(channel_path.parent))
    
        # Get Metadata
        df_meta = self.df_metadata.get(on_or_off)
        if df_meta is None:
            raise RuntimeError("Must run build_metadata_index() first.")
    
        results = []
        n_rows = len(df_meta)
    
        for idx, row in df_meta.iterrows():
            percent = ((idx + 1) / n_rows) * 100
            start = row['sample_index']
            if idx < len(df_meta) - 1:
                end = df_meta.iloc[idx + 1]['sample_index']
            else:
                _, end = drf_reader.get_bounds(self.channel)
    
            try:
                blocks = drf_reader.get_continuous_blocks(start, end - 1, self.channel)
            except Exception as e:
                print(f"... Failed to read blocks at row {idx}: {e}")
                blocks = {}
    
            print(f"{on_or_off} state, Row {idx+1} / {n_rows} ({percent:.1f}%): "
                  f"Index Range [{start}, {end}) fc={row['center_frequency']/1e9:.2f} GHz "
                  f"{len(blocks)} block(s)")
    
            iq_combined = np.array([])
            if blocks:
                row_data = []
                for j, (b_start, b_len) in enumerate(blocks.items()):
                    try:
                        iq = drf_reader.read_vector(b_start, b_len, self.channel)
                        row_data.append(iq)
                        print(f"... ... Block {j}: start={b_start}, len={b_len}")
                    except Exception as e:
                        print(f"... ... Block {j} failed: {e}")
                if row_data:
                    iq_combined = np.concatenate(row_data, axis=0)
    
            if iq_combined.size == 0:
                stats = (np.nan,) * 4
                num_nans = np.nan
                pct_nans = np.nan
                total_samples = 0
                total_duration_s = np.nan
            else:
                num_nans = np.isnan(iq_combined).sum()
                pct_nans = (num_nans / len(iq_combined)) * 100
                fs = row['sample_rate_numerator'] / row['sample_rate_denominator']
                stats = IQAnalyzer.compute_power_stats(iq_combined, method='fft_bandpass', fs=fs, f_notch=50e3)
                total_samples = len(iq_combined)
                total_duration_s = len(iq_combined) / fs if fs else np.nan
    
            results.append({
                'row_index': idx,
                'f_center_GHz': row['center_frequency'] / 1e9,
                'IQ_power_mean': stats[0],
                'IQ_power_std': stats[1],
                'IQ_power_min': stats[2],
                'IQ_power_max': stats[3],
                'num_continuous_blocks': len(blocks),
                'total_samples': total_samples,
                'num_nans': num_nans,
                'pct_nans': pct_nans,
                'total_duration_s': total_duration_s,
            })
    
            if save_spectrograms and iq_combined.size > 0:
                self.dir_spectrograms = self.dir_output / "spectrograms"
                self.dir_spectrograms.mkdir(exist_ok=True)
                save_name = self.dir_spectrograms / f"{row['center_frequency']/1e9:.2f}GHz_row{idx}_{on_or_off}_{self.base_name}.png"
                fig, ax = IQAnalyzer.plot_waterfall(iq_combined, fs=fs, fc=row['center_frequency'],
                                                    save_file=str(save_name))
                plt.close(fig)
    
        self.df_stats[on_or_off] = pd.DataFrame(results)
        cache_file = cache_file or f"stats_{on_or_off}_{self.base_name}.csv"
        cache_path = self.dir_output / cache_file
        self.df_stats[on_or_off].to_csv(cache_path, index=False)
        print(f"... [Stats] Saved stats to {cache_path}")
        return self.df_stats[on_or_off]

    def compute_noisefigure(self, df_on=None, df_off=None, diode=None):
        """Compute Noise Figure using ON/OFF power stats and a noise diode."""

        # Resolve diode (allow override)
        diode = diode or self.diode

        # Ensure diode YAML saved alongside results
        diode_yaml_file = self.dir_output / f"diode_{diode.model}_{diode.serial}.yaml"
        diode.save_yaml(diode_yaml_file)
        setattr(diode, "yaml_file", str(diode_yaml_file))  # attach for reference

        # Resolve ON/OFF data
        df_on = df_on if df_on is not None else self.df_stats.get("on")
        df_off = df_off if df_off is not None else self.df_stats.get("off")

        if df_on is None or df_off is None:
            raise RuntimeError("Must provide or compute df_on and df_off first.")

        df = pd.merge(df_on, df_off, on="f_center_GHz",
                      suffixes=("_on", "_off"), how="outer")

        results = []
        for _, row in df.iterrows():
            if pd.isna(row['IQ_power_mean_on']) or pd.isna(row['IQ_power_mean_off']):
                nf_result = {
                    'frequency_GHz': row['f_center_GHz'],
                    'ENR_dB': np.nan,
                    'Y': np.nan,
                    'NF_dB': np.nan,
                    'P_on': row['IQ_power_mean_on'],
                    'P_off': row['IQ_power_mean_off']
                }
            else:
                nf_result = diode.compute_noisefigure(
                    P_on=row['IQ_power_mean_on'],
                    P_off=row['IQ_power_mean_off'],
                    freq_ghz=row['f_center_GHz']
                )
            results.append(nf_result)
        
        self.df_noisefigure = pd.DataFrame(results)

        # Save Noise Figure results
        cache_path = self.dir_output / f"df_noisefigure_{self.base_name}.csv"
        self.df_noisefigure.to_csv(cache_path, index=False)
        print(f"... [NoiseFigure] Saved results to {cache_path}")
        print(f"... [NoiseFigure] Saved diode YAML to {diode_yaml_file}")

        return self.df_noisefigure

    @staticmethod
    def highlight_missing_data(ax, x_data, y_data, color='lightgray', alpha=0.5):
        """
        Shade regions where data is missing (NaN) in a plot.
        """
        nan_indices = y_data.isna()
        nan_groups = []
        start = None
        for i in range(len(nan_indices)):
            if nan_indices.iloc[i]:
                if start is None:
                    start = i
            elif start is not None:
                nan_groups.append((start, i - 1))
                start = None
        if start is not None:
            nan_groups.append((start, len(nan_indices) - 1))
        for start, end in nan_groups:
            ax.axvspan(x_data.iloc[start], x_data.iloc[end], color=color, alpha=alpha)
            
    def plot_noisefigure_summary(self, df_noisefigure=None, plots=None):
        df_noisefigure = df_noisefigure if df_noisefigure is not None else self.df_noisefigure
        if df_noisefigure is None:
            raise RuntimeError("Must compute noisefigure results first.")
    
        if plots is None:
            plots = [ #Column, title, color, label
                ("NF_dB", "Noise Figure (dB)", "b", "Noise Figure (dB)"),
                ("Y", "Y Factor", "g", "Y Factor"),
                ("P_on", "P_on", "r", "Mean(abs(IQ)) Noise Diode ON"),
                ("P_off", "P_off", "m", "Mean(abs(IQ)) Noise Diode OFF"),
            ]
    
        fig, ax = plt.subplots(len(plots), 1, figsize=(10, 3 * len(plots)), sharex=True)
        if len(plots) == 1:
            ax = [ax]
    
        for i, (col, ylabel, color, title) in enumerate(plots):
            if col not in df_noisefigure.columns:
                print(f"... Skipping {col}, not found in DataFrame.")
                continue
    
            ax[i].plot(df_noisefigure['frequency_GHz'], df_noisefigure[col],
                       marker='o', linestyle='None', color=color)
            self.highlight_missing_data(ax[i], df_noisefigure['frequency_GHz'], df_noisefigure[col])
            ax[i].set(ylabel=ylabel, title=title)
    
        ax[-1].set_xlabel('Frequency (GHz)')
        plt.tight_layout()
    
        save_path = self.dir_output / f"noisefigure_summary_{self.base_name}.png"
        fig.savefig(save_path, dpi=300)
        print(f"... [NoiseFigure Summary] Saved to {save_path}")
    
        return fig, ax





# ===== EXECUTION ===== #
def bookmark_main(): pass

if __name__ == "__main__":

    # ===== NOISE DIODE ===== #
    ENR_TABLE = {
        0.01: 15.5, 0.10:15.74, 1: 15.28, 2: 14.87, 3: 14.66, 4: 14.59, 5: 14.54,
        6: 14.65, 7: 14.81, 8: 14.85, 9: 14.99, 10: 15.36, 11: 15.23, 12: 15.22,
        13: 15.36, 14: 15.25, 15: 14.91, 16: 14.79, 17: 14.98, 18: 14.61
    }
    diode = NoiseDiode("346B", "37502", ENR_TABLE)

    # ===== NOISE FIGURE CALCULATION ===== #
    # Set up the noise figure class, no calculations (yet)
    pipeline = YFactorPipeline(
        dir_dataset="/media/unknowndevice/d6146e64-be53-46f2-a627-5e690a0c8b4a/data/recordings/new/MEP10_CAL_20250727_TUNER_AFE_RFSOC/",
        channel = 'chA', # DigitalRF channel name
        dir_template=str(Path("{dir_dataset}") / "{on_or_off}" / "sr10MHz" / "{channel}"), # Subfolder structure
        diode=diode, # Instance of NoiseDiode
        dir_output="/data/outputs/"
    )
    
    # Plot Diode ENR Curve
    fig_diode, ax_diode = diode.plot_enr(dir_save=pipeline.dir_output)
    
    # Check File availability and plot summary
    list_digitalrf_data_on  = pipeline.check_digitalrf_files("on")
    list_digitalrf_data_off = pipeline.check_digitalrf_files("off")

    # Build Metadata Index and log errors
    df_metadata_on,  df_metadata_errors_on  = pipeline.build_metadata_index("on")
    df_metadata_off, df_metadata_errors_off = pipeline.build_metadata_index("off")

    # Loop through each frequency bin, compute stats (min, max, mean, std), and spectrograms
    #   This can take a while. save_spectrograms=False makes it go slightly faster.
    df_on  = pipeline.compute_stats("on" , save_spectrograms=True)
    df_off = pipeline.compute_stats("off", save_spectrograms=True)

    # Compute Noise Figure
    df_noisefigure = pipeline.compute_noisefigure(df_on=None, df_off=None) #Uses df_on, df_off from pipeline.compute_stats() unless you want to override with pd.read_csv(

    # Summary Plot (default: NF_dB, Y, P_on, P_off)
    fig_summary, ax_summary = pipeline.plot_noisefigure_summary(df_noisefigure=None) # Override data to plot with df_noisefigure = pd.read_csv("df_noisefigure.csv")
