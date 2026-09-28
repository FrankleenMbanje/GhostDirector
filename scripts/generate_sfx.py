"""
GhostDirector — Sound Design Studio (CC0 SFX Generator)

Generates broadcast-quality, royalty-free audio effects using pure numpy and wave:
- whoosh_transition.wav (dynamic stereo air sweep)
- sub_impact_boom.wav (cinematic 40Hz sub-bass revelation drop)
- camera_shutter.wav (crisp mechanical dual-click)
- tension_riser.wav (ascending harmonic tension swell)
"""

import math
import wave
import struct
from pathlib import Path
import numpy as np

SAMPLE_RATE = 44100


def _save_wav(filename: Path, stereo_signal: np.ndarray):
    """Save float32 array [-1.0, 1.0] as 16-bit PCM stereo WAV."""
    filename.parent.mkdir(parents=True, exist_ok=True)
    peak = np.max(np.abs(stereo_signal))
    if peak > 0:
        stereo_signal = (stereo_signal / peak) * 0.89

    int_signal = (stereo_signal * 32767.0).astype(np.int16)

    with wave.open(str(filename), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        interleaved = np.empty((int_signal.shape[1] * 2,), dtype=np.int16)
        interleaved[0::2] = int_signal[0]
        interleaved[1::2] = int_signal[1]
        wf.writeframes(interleaved.tobytes())


def generate_whoosh(out_path: Path):
    """
    Generates an organic cinematic air whoosh for transitions.
    Uses multi-stage filtered noise with dynamic cutoff sweep (ZERO sine waves to prevent whistling).
    """
    duration = 0.45
    n_samples = int(duration * SAMPLE_RATE)
    t = np.linspace(0, duration, n_samples, endpoint=False)

    # Pure Gaussian noise for organic texture
    white_noise = np.random.normal(0, 1.0, n_samples)

    # Smooth parabolic air-rush envelope
    env = np.sin(np.pi * (t / duration)) ** 2.2

    # Moving average filter with dynamic window to simulate sweeping lowpass filter
    # Window starts large (dark), shrinks in middle (bright air rush), expands at end (dissipates)
    window_sizes = (40 - 32 * np.sin(np.pi * (t / duration))).astype(int)
    window_sizes = np.clip(window_sizes, 4, 45)

    smoothed = np.zeros(n_samples)
    # Block-based filtering for organic air sound
    block_size = 256
    for b in range(0, n_samples, block_size):
        end_b = min(b + block_size, n_samples)
        w_size = window_sizes[b]
        kernel = np.ones(w_size) / w_size
        chunk_start = max(0, b - w_size)
        chunk_end = min(n_samples, end_b + w_size)
        conv = np.convolve(white_noise[chunk_start:chunk_end], kernel, mode="same")
        offset_start = b - chunk_start
        smoothed[b:end_b] = conv[offset_start : offset_start + (end_b - b)]

    combined = smoothed * env

    # Stereo dynamic panning (Left to Right sweep)
    pan_l = np.cos((np.pi / 2) * (t / duration))
    pan_r = np.sin((np.pi / 2) * (t / duration))

    signal = np.vstack([combined * pan_l, combined * pan_r])
    _save_wav(out_path, signal)


def generate_sub_impact(out_path: Path):
    """Generates a deep warm 40Hz cinematic hit for shocking revelations."""
    duration = 0.85
    n_samples = int(duration * SAMPLE_RATE)
    t = np.linspace(0, duration, n_samples, endpoint=False)

    # Warm sub-bass fundamental with fast pitch drop from 55Hz to 32Hz
    freq = 32.0 + 25.0 * np.exp(-14.0 * t)
    phase = np.cumsum(2.0 * np.pi * freq / SAMPLE_RATE)
    sub = np.sin(phase)

    # Gentle low-frequency noise body for acoustic weight
    noise = np.random.normal(0, 0.4, n_samples)
    noise_kernel = np.ones(30) / 30.0
    filtered_noise = np.convolve(noise, noise_kernel, mode="same")

    env = np.exp(-4.5 * t)
    attack = int(0.006 * SAMPLE_RATE)
    env[:attack] *= np.linspace(0, 1, attack)

    combined = (sub * 0.85 + filtered_noise * 0.25) * env
    signal = np.vstack([combined, combined])
    _save_wav(out_path, signal)


def generate_camera_shutter(out_path: Path):
    """Generates a crisp mechanical camera shutter click for photos & documents."""
    duration = 0.20
    n_samples = int(duration * SAMPLE_RATE)
    t = np.linspace(0, duration, n_samples, endpoint=False)

    signal_l = np.zeros(n_samples)
    signal_r = np.zeros(n_samples)

    def add_click(start_t: float, amp: float, center_freq: float):
        start_idx = int(start_t * SAMPLE_RATE)
        burst_len = int(0.025 * SAMPLE_RATE)
        if start_idx + burst_len > n_samples:
            burst_len = n_samples - start_idx
        local_t = np.linspace(0, burst_len / SAMPLE_RATE, burst_len, endpoint=False)
        click_env = np.exp(-220.0 * local_t)
        click_noise = np.random.normal(0, 1.0, burst_len) * click_env * amp
        signal_l[start_idx : start_idx + burst_len] += click_noise
        signal_r[start_idx : start_idx + burst_len] += click_noise * 0.9

    add_click(0.015, 0.9, 1800.0)
    add_click(0.070, 0.7, 1400.0)

    signal = np.vstack([signal_l, signal_r])
    _save_wav(out_path, signal)


def generate_tension_riser(out_path: Path):
    """
    Generates a dark cinematic atmospheric swell.
    Uses filtered noise swell and warm low-end rumble (NO high-pitched whistle sines).
    """
    duration = 1.2
    n_samples = int(duration * SAMPLE_RATE)
    t = np.linspace(0, duration, n_samples, endpoint=False)

    # Warm low-frequency drone (constant 65Hz root, not high frequency sweep)
    drone = 0.3 * np.sin(2.0 * np.pi * 65.0 * t)

    # Textured white noise swell
    noise = np.random.normal(0, 0.6, n_samples)
    noise_kernel = np.ones(18) / 18.0
    filtered_noise = np.convolve(noise, noise_kernel, mode="same")

    # Exponential crescendo envelope
    env = (t / duration) ** 2.5
    mute_tail = int(0.04 * SAMPLE_RATE)
    env[-mute_tail:] *= np.linspace(1, 0, mute_tail)

    combined = (drone * 0.4 + filtered_noise * 0.6) * env
    signal = np.vstack([combined * 0.95, combined])
    _save_wav(out_path, signal)


def generate_all_sfx(output_dir: Path | None = None) -> dict[str, Path]:
    """Generates all 4 standard studio SFX files and returns dict of paths."""
    if output_dir is None:
        output_dir = Path(__file__).parent.parent / "assets" / "sfx"
    output_dir.mkdir(parents=True, exist_ok=True)

    sfx_files = {
        "whoosh": output_dir / "whoosh_transition.wav",
        "sub_impact": output_dir / "sub_impact_boom.wav",
        "camera_shutter": output_dir / "camera_shutter.wav",
        "tension_riser": output_dir / "tension_riser.wav",
    }

    generate_whoosh(sfx_files["whoosh"])
    generate_sub_impact(sfx_files["sub_impact"])
    generate_camera_shutter(sfx_files["camera_shutter"])
    generate_tension_riser(sfx_files["tension_riser"])

    return sfx_files


if __name__ == "__main__":
    out = Path(__file__).parent.parent / "assets" / "sfx"
    results = generate_all_sfx(out)
    for name, path in results.items():
        print(f"Generated: {name} -> {path} ({path.stat().st_size} bytes)")
