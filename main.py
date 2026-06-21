# main.py - Pico 2 Drum Module with WAV playback and biquad filter
# Hardware: PCM5102A (I2S DAC), ADS7828 (I2C ADC), Trigger (GP14), Button (GP15)
#
# 8 potentiometers: Volume, Tone (Pitch), Cutoff, Resonance, Attack, Decay, Sustain, Release
#
# IMPORTANT - version requirement:
#   Requires CircuitPython >= 9.2.5 (audiodelays.PitchShift was only added in
#   9.2.5 - older 9.x releases will raise an ImportError).
#   No extra libraries needed - everything used here is part of the
#   CircuitPython firmware (adafruit_wave is no longer required).
#
# Architecture note:
#   Each module only has a single voice (one RP2350 = one sample, e.g. kick,
#   snare, hi-hat - multiple modules together form the drum kit). A new
#   trigger immediately cuts off any hit that's still playing and restarts
#   (choke behavior, typical for a mono drum voice).
#
# Architecture note on "Tone":
#   Samples are streamed directly from the file as usual via
#   audiocore.WaveFile (audiomixer.Mixer) - no loading into RAM, no length
#   restriction. Pitch is controlled via a separate real-time effect
#   (audiodelays.PitchShift) in the signal path, which is independent of
#   synthio's wavetable limit (synthio.waveform_max_length, ~16384 samples).
#   Trade-off: the effect processes audio in a granular fashion using
#   window/overlap buffers, which adds a small, constant amount of latency.
#
# Limitations of this version:
#   - Stereo samples are supported 1:1 (no mono downmix needed, since we no
#     longer go through synthio waveforms)
#   - Sample length is essentially unlimited (only constrained by flash
#     storage space)

import time
import board
import busio
import audiobusio
import audiocore
import audiomixer
import audiodelays
import audiofilters
import synthio
from digitalio import DigitalInOut, Direction, Pull

# ----- CONFIGURATION -----
# I2C (ADS7828) - adjust pins as needed!
I2C_SCL_PIN = board.GP1
I2C_SDA_PIN = board.GP0
ADS7828_ADDR = 0x48

# I2S (PCM5102A)
I2S_BCK_PIN = board.GP16
I2S_LRCK_PIN = board.GP17
I2S_DIN_PIN = board.GP18
SAMPLE_RATE = 44100
CHANNELS = 2          # must match your WAV files (mono -> 1, stereo -> 2)
# Determines the baseline latency of Mixer/PitchShift/Filter (BUFFER_SIZE/SAMPLE_RATE).
# 512 ≈ 11.6ms instead of the previous 1024 ≈ 23ms. Increase again if you hear
# dropouts/clicks; 256 can be tried if playback stays stable.
BUFFER_SIZE = 512

# Inputs
TRIGGER_PIN = board.GP14
BUTTON_PIN = board.GP15
NUM_SAMPLES = 16       # number of your WAV files

POT_UPDATE_INTERVAL = 0.02     # only poll pots every 20ms (I2C is comparatively slow)
RETRIGGER_LOCKOUT = 0.003      # minimum gap between two triggers (piezo bounce)
BUTTON_EDGE_DEBOUNCE = 0.02    # debounce time for the button
LONG_PRESS_THRESHOLD = 1.0     # hold duration after which a press counts as "long"
TONE_DEADZONE = 0.03           # range around pot center treated as "no transposition"

# ----- ADS7828 DRIVER -----
# Important: the ADS7828 does NOT address its 8 single-ended channels
# linearly via the C2/C1/C0 bits in the command byte - it uses a fixed,
# "interleaved" order according to the datasheet:
#   Channel:      0  1  2  3  4  5  6  7
#   C2C1C0 value: 0  4  1  5  2  6  3  7
_ADS7828_CH_MAP = (0, 4, 1, 5, 2, 6, 3, 7)


class ADS7828:
    def __init__(self, i2c, addr=ADS7828_ADDR):
        self.i2c = i2c
        self.addr = addr

    def read_channel(self, channel):
        if not 0 <= channel <= 7:
            return 0
        code = _ADS7828_CH_MAP[channel]
        # 0x8C = SD=1 (single-ended), PD1=1, PD0=1 (reference + ADC always
        # active between conversions -> no wake-up delay needed)
        command = 0x8C | (code << 4)
        result = bytearray(2)
        try:
            # writeto_then_readfrom performs the write (channel select) and
            # the read in a single transaction (repeated start). At 100-400kHz
            # the bus timing alone already comfortably exceeds the ADS7828's
            # conversion time, so no additional sleep is necessary.
            self.i2c.writeto_then_readfrom(self.addr, bytes([command]), result)
            raw = ((result[0] & 0x0F) << 8) | result[1]
            return raw << 4  # normalized to 16 bit (0-65535)
        except OSError as e:
            print(f"ADS7828 I2C error on channel {channel}: {e}")
            return 0


# ----- ENVELOPE STATE FOR THE VOICE -----
class Voice:
    __slots__ = ("stage", "elapsed", "level")

    def __init__(self):
        self.stage = "idle"   # idle -> attack -> decay -> release -> idle
        self.elapsed = 0.0
        self.level = 0.0


# ----- SOUND ENGINE with streamed WAV playback, pitch effect & filter -----
class DrumEngine:
    def __init__(self, audio_out, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.audio_out = audio_out
        self.current_sample_index = 0

        # Parameters (controlled by potentiometers)
        self.volume = 0.5
        self.tone = 0.5         # 0..1, 0.5 = original pitch
        self.cutoff = 5000.0
        self.resonance = 1.0
        self.attack = 0.01
        self.decay = 0.2
        self.sustain = 0.8
        self.release = 0.3

        # The mixer handles the actual WAV playback (streamed from the
        # file). Only a single voice, since each module ever triggers only
        # one sample (kick, snare, ...). A new trigger automatically cuts
        # off a hit that's still playing and restarts immediately (choke
        # behavior, as is typical for most mono drum voices).
        self.mixer = audiomixer.Mixer(
            voice_count=1,
            buffer_size=BUFFER_SIZE,
            channel_count=CHANNELS,
            bits_per_sample=16,
            samples_signed=True,
            sample_rate=sample_rate,
        )

        # Pitch effect: only patched into the signal chain when the Tone pot
        # actually deviates from center (see update_pitch). In the normal
        # case (Tone centered) the chain runs without PitchShift, saving its
        # window/overlap latency entirely. window/overlap are already
        # reduced here for lower latency - increase again (e.g. window=1024,
        # overlap=128) if you hear artifacts at strong transposition.
        self.pitch_shift = audiodelays.PitchShift(
            semitones=0.0,
            mix=1.0,
            window=512,
            overlap=64,
            buffer_size=BUFFER_SIZE,
            channel_count=CHANNELS,
            sample_rate=sample_rate,
            bits_per_sample=16,
            samples_signed=True,
        )

        # Biquad filter (low-pass)
        self.filter = audiofilters.Filter(
            filter=synthio.Biquad(
                synthio.FilterMode.LOW_PASS,
                frequency=self.cutoff,
                Q=self.resonance,
            ),
            mix=1.0,
            buffer_size=BUFFER_SIZE,
            channel_count=CHANNELS,
            sample_rate=sample_rate,
        )

        # PitchShift is wired up to the mixer right away regardless (so it's
        # ready to go the moment it's needed) - but what's actually heard is
        # only whatever audio_out is currently pulling via filter.play(...).
        self.pitch_shift.play(self.mixer)
        self.filter.play(self.mixer)  # default: PitchShift bypassed
        self.audio_out.play(self.filter)
        self._pitch_active = False

        self.voice = Voice()

        self.wav_sample = None
        self.wav_file = None

    def load_sample(self, index):
        """Loads a WAV file from flash and replaces the current sample."""
        filename = f"/samples/sample_{index:02d}.wav"
        try:
            new_file = open(filename, "rb")
            new_wave = audiocore.WaveFile(new_file)
        except OSError as e:
            print(f"Error loading sample {index}: {e}")
            return False

        # Stop the voice BEFORE closing the old file. Otherwise it could
        # still access an already-closed file (click/crash). This does cost
        # a currently playing hit when switching samples, but it's the safe
        # approach.
        self.mixer.voice[0].stop()
        self.voice.stage = "idle"
        self.voice.level = 0.0

        if self.wav_file is not None:
            try:
                self.wav_file.close()
            except OSError:
                pass

        self.wav_file = new_file
        self.wav_sample = new_wave
        self.current_sample_index = index
        print(f"Sample {index} loaded: {filename}")
        return True

    def trigger(self):
        """Plays the sample and restarts the envelope from the beginning.
        Any hit still playing is cut off immediately (mono voice, choke
        behavior)."""
        if self.wav_sample is None:
            return
        self.mixer.voice[0].play(self.wav_sample, loop=False)
        self.voice.stage = "attack"
        self.voice.elapsed = 0.0

    def _update_voice_envelope(self, v, dt):
        # One-shot envelope for drum hits: Attack -> Decay -> Release.
        # No "sustain" as an indefinite hold state, since a trigger input
        # never delivers a note-off - the sustain value only determines the
        # starting level of the release stage.
        if v.stage == "idle":
            v.level = 0.0
            return

        v.elapsed += dt

        if v.stage == "attack":
            if self.attack <= 0.0 or v.elapsed >= self.attack:
                v.level = 1.0
                v.stage = "decay"
                v.elapsed = 0.0
            else:
                v.level = v.elapsed / self.attack

        elif v.stage == "decay":
            if self.decay <= 0.0 or v.elapsed >= self.decay:
                v.level = self.sustain
                v.stage = "release"
                v.elapsed = 0.0
            else:
                v.level = 1.0 - (1.0 - self.sustain) * (v.elapsed / self.decay)

        elif v.stage == "release":
            if self.release <= 0.0 or v.elapsed >= self.release:
                v.level = 0.0
                v.stage = "idle"
            else:
                v.level = self.sustain * (1.0 - v.elapsed / self.release)

    def update(self, dt):
        """Call this every loop iteration: updates the envelope and applies
        the resulting level to the mixer voice."""
        if self.voice.stage != "idle":
            self._update_voice_envelope(self.voice, dt)
            self.mixer.voice[0].level = self.voice.level * self.volume

    def update_filter(self):
        """Updates the biquad filter with the current cutoff/resonance
        values. Deliberately called less often (only on pot updates), since
        re-creating the Biquad object would cause unnecessary overhead in
        the time-critical audio loop."""
        self.filter.filter = synthio.Biquad(
            synthio.FilterMode.LOW_PASS,
            frequency=max(20.0, min(20000.0, self.cutoff)),
            Q=max(0.5, self.resonance),
        )

    def update_pitch(self):
        """Updates the pitch-shift effect from the Tone pot.
        0.5 = original pitch, range is +/-12 semitones (+/-1 octave).
        Inside the dead zone around center (TONE_DEADZONE), PitchShift is
        removed from the chain entirely to save its latency in the normal
        case. The switch only happens while no voice is currently playing
        (self.voice.stage == "idle"); otherwise switching would cause a
        brief click due to the jump in internal buffer state."""
        centered = abs(self.tone - 0.5) < TONE_DEADZONE

        if centered:
            if self._pitch_active and self.voice.stage == "idle":
                self.filter.play(self.mixer)
                self._pitch_active = False
        else:
            self.pitch_shift.semitones = (self.tone - 0.5) * 24.0
            if not self._pitch_active and self.voice.stage == "idle":
                self.filter.play(self.pitch_shift)
                self._pitch_active = True


# ----- MAIN PROGRAM -----
def main():
    print("Drum module starting...")

    # I2C (ADS7828)
    i2c = busio.I2C(I2C_SCL_PIN, I2C_SDA_PIN)
    while not i2c.try_lock():
        pass
    # Bus is used exclusively for the ADS7828 -> intentionally never unlocked
    adc = ADS7828(i2c)

    # I2S audio (PCM5102A)
    i2s_out = audiobusio.I2SOut(
        bit_clock=I2S_BCK_PIN,
        word_select=I2S_LRCK_PIN,
        data=I2S_DIN_PIN,
    )

    # Initialize the sound engine
    engine = DrumEngine(i2s_out)
    engine.load_sample(0)

    # Initialize inputs
    trigger_pin = DigitalInOut(TRIGGER_PIN)
    trigger_pin.direction = Direction.INPUT
    trigger_pin.pull = Pull.DOWN

    button_pin = DigitalInOut(BUTTON_PIN)
    button_pin.direction = Direction.INPUT
    button_pin.pull = Pull.UP

    current_sample_index = 0
    last_trigger_state = False
    last_trigger_time = 0.0
    last_pot_time = 0.0
    last_loop_time = time.monotonic()

    # Button state for short-press/long-press detection
    button_raw_state = True       # pull-up: True = not pressed
    button_stable_state = True
    button_last_change_time = 0.0
    button_press_start = 0.0
    button_long_fired = False

    print("Ready. Trigger and button active...")

    while True:
        now = time.monotonic()
        dt = now - last_loop_time
        last_loop_time = now

        # --- Trigger: rising edge, non-blocking ---
        trig_state = trigger_pin.value
        if (
            trig_state
            and not last_trigger_state
            and (now - last_trigger_time) > RETRIGGER_LOCKOUT
        ):
            engine.trigger()
            last_trigger_time = now
        last_trigger_state = trig_state

        # --- Button: debounced, with short-press/long-press distinction ---
        # Short press (< LONG_PRESS_THRESHOLD): triggers the current sample
        # with the current pot settings on release.
        # Long press (>= LONG_PRESS_THRESHOLD): as soon as the threshold is
        # reached (still held down), immediately switches to the next
        # sample - no additional trigger on release.
        raw = button_pin.value
        if raw != button_raw_state:
            button_raw_state = raw
            button_last_change_time = now
        elif (
            raw != button_stable_state
            and (now - button_last_change_time) > BUTTON_EDGE_DEBOUNCE
        ):
            button_stable_state = raw
            if not button_stable_state:
                # Edge: button was pressed
                button_press_start = now
                button_long_fired = False
            else:
                # Edge: button was released
                if not button_long_fired:
                    engine.trigger()

        if (
            not button_stable_state
            and not button_long_fired
            and (now - button_press_start) >= LONG_PRESS_THRESHOLD
        ):
            current_sample_index = (current_sample_index + 1) % NUM_SAMPLES
            engine.load_sample(current_sample_index)
            button_long_fired = True

        # --- Pots (ADS7828): polled decoupled from the audio loop ---
        if (now - last_pot_time) > POT_UPDATE_INTERVAL:
            last_pot_time = now

            pot_volume = adc.read_channel(0) / 65535.0
            pot_tone = adc.read_channel(1) / 65535.0
            pot_cutoff = adc.read_channel(2) / 65535.0
            pot_resonance = adc.read_channel(3) / 65535.0
            pot_attack = adc.read_channel(4) / 65535.0
            pot_decay = adc.read_channel(5) / 65535.0
            pot_sustain = adc.read_channel(6) / 65535.0
            pot_release = adc.read_channel(7) / 65535.0

            engine.volume = pot_volume
            engine.tone = pot_tone
            engine.cutoff = 20.0 + pot_cutoff * 19980.0
            engine.resonance = 0.5 + pot_resonance * 9.5
            engine.attack = 0.005 + pot_attack * 2.0
            engine.decay = 0.005 + pot_decay * 2.0
            engine.sustain = pot_sustain
            engine.release = 0.005 + pot_release * 2.0

            engine.update_filter()
            engine.update_pitch()

        # --- Update the envelope every loop using the real elapsed time ---
        engine.update(dt)

        # No time.sleep() here: the loop runs at full speed so the trigger
        # edge (GP14) is detected as quickly as possible. Costs a bit more
        # power/CPU usage, which is not an issue for a dedicated mono drum
        # module.


if __name__ == "__main__":
    main()
