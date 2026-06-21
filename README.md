## untested! ##

# pico-drums
Sample-player for Eurorack with RP2350/Pico2

# Developement is still in progress

# Pico Drum Module

A CircuitPython-based drum module with WAV sample playback, pitch shifting,
a low-pass filter, and a full ADSR envelope, all controlled via 8
potentiometers. Each module is a single-voice (mono) drum sound - build
several to form a full kit (kick, snare, hi-hat, ...).

## Hardware

| Component | Function |
|---|---|
| PCM5102A | I2S DAC for audio output |
| ADS7828 | 8-channel I2C ADC for the potentiometers |
| Trigger input | digital input, e.g. from a piezo/pad with external threshold circuitry |
| Button | trigger sample (short press) / switch sample (long press) |
| Board | Raspberry Pi Pico 2 (RP2350) |

### Pinout

| Pin | Function |
|---|---|
| GP0 | I2C SDA (ADS7828) |
| GP1 | I2C SCL (ADS7828) |
| GP14 | Trigger input (pull-down, rising edge = hit) |
| GP15 | Button (pull-up, switches to GND) |
| GP16 | I2S bit clock (PCM5102A BCK) |
| GP17 | I2S word select (PCM5102A LRCK) |
| GP18 | I2S data (PCM5102A DIN) |

The ADS7828 address is `0x48` (default address, ADDR pins tied to GND).

## Software requirements

- **CircuitPython 9.2.5 or newer** (the `audiodelays.PitchShift` module used
  for pitch shifting was only introduced in 9.2.5)
- No extra libraries required - everything used is part of the CircuitPython
  firmware

## File layout on the board

```
CIRCUITPY/
├── code.py            (or main.py)
└── samples/
    ├── sample_00.wav
    ├── sample_01.wav
    ├── sample_02.wav
    ├── ...
    └── sample_15.wav
```

Samples live in the `samples/` folder at the root of the CIRCUITPY drive and
are simply copied over via drag & drop.

### Sample file requirements

- Format: standard PCM WAV (mono or stereo - both are supported directly,
  since playback is streamed rather than loaded into RAM)
- Sample rate: should match `SAMPLE_RATE` in the code (44100 Hz by default)
- Length: effectively unlimited, only constrained by available flash storage
- File names exactly `sample_00.wav` through `sample_15.wav` (two digits,
  zero-padded) - adjust `NUM_SAMPLES` in the code if you have fewer

## Usage

### Trigger input (GP14)

A rising edge triggers the currently loaded sample using the current
potentiometer settings. A short lockout (3 ms) prevents multiple triggers
from piezo bounce.

### Button (GP15)

| Press duration | Action |
|---|---|
| short (< 1 s) | on release, triggers the current sample with the current pot settings (same as the trigger input) |
| long (>= 1 s) | switches to the next sample as soon as the threshold is reached (while still held down) |

### Potentiometers (ADS7828 channels)

| Channel | Function | Range |
|---|---|---|
| 0 | Volume | 0.0 - 1.0 |
| 1 | Tone (Pitch) | +/-1 octave, center = original sample pitch |
| 2 | Cutoff (filter) | 20 Hz - 20 kHz |
| 3 | Resonance (filter) | Q 0.5 - 10 |
| 4 | Attack | 5 ms - ~2 s |
| 5 | Decay | 5 ms - ~2 s |
| 6 | Sustain | 0.0 - 1.0 (level) |
| 7 | Release | 5 ms - ~2 s |

Pots are polled decoupled from the audio loop, every 20 ms
(`POT_UPDATE_INTERVAL`), since I2C transactions are comparatively slow.

## Architecture overview

- Samples are streamed directly from flash via `audiocore.WaveFile` through
  a single-voice `audiomixer.Mixer` - nothing is loaded into RAM, so there's
  no practical limit on sample length.
- A custom one-shot ADSR envelope drives the mixer voice's `level` each loop
  iteration (Attack -> Decay -> Release; there's no indefinite "sustain"
  hold, since a drum trigger never produces a note-off).
- Pitch is implemented via `audiodelays.PitchShift`, a real-time granular
  pitch-shift effect. It's only patched into the signal chain when the Tone
  pot deviates from its center position (outside `TONE_DEADZONE`) - when
  centered, the signal path skips it entirely (`Mixer -> Filter -> Output`)
  to avoid its window/overlap latency in the common case.
- A `audiofilters.Filter` (low-pass biquad) shapes the tone, controlled by
  the Cutoff/Resonance pots.
- Since each module only ever plays one voice, a new trigger immediately
  cuts off ("chokes") any hit still playing and restarts from the beginning.

## Latency tuning

The main knobs affecting trigger-to-sound latency:

- `BUFFER_SIZE` (default 512, ~11.6 ms at 44.1kHz) - the baseline latency of
  the whole audio chain (Mixer/PitchShift/Filter). Lower it for less
  latency, raise it if you hear dropouts/clicks.
- `window` / `overlap` in `audiodelays.PitchShift` (default 512/64) - only
  relevant while actually transposing (Tone pot off-center). Lower values
  reduce latency at the cost of more granular artifacts on strongly
  transposed sounds.
- `TONE_DEADZONE` - how close to center the Tone pot needs to be before
  `PitchShift` is bypassed entirely.

## Known limitations

- Switching samples (long button press) stops the currently playing voice,
  since both share the same mixer voice.
- `PitchShift` only switches in/out of the signal chain while the voice is
  idle, to avoid an audible click - moving the Tone pot in/out of its dead
  zone while a hit is playing won't take effect until the next trigger.
- `audiodelays.PitchShift` adds a small, constant latency whenever the Tone
  pot is off-center, due to its window/overlap-based processing.

## Troubleshooting

| Problem | Possible cause |
|---|---|
| No sound | Check I2S wiring (BCK/LRCK/DIN), confirm sample files exist, check the serial console for errors |
| `ImportError: no module named 'audiodelays'` | CircuitPython version is older than 9.2.5 - update the firmware |
| Pots seem swapped/wrong | Check ADS7828 wiring (SDA/SCL) and I2C address (`0x48`) |
| Sample won't load | Check filename/path (`/samples/sample_NN.wav`) |
| Audio clicks/dropouts | Increase `BUFFER_SIZE`, or increase `window`/`overlap` in `PitchShift` if it only happens while transposing |
| No serial output at all | Make sure the file is named `code.py` or `main.py`, connect the serial console before the board boots (or press Ctrl+D in the console to soft-reset) |
