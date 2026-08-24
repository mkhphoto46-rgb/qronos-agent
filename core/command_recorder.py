from __future__ import annotations

import math
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from core.audio_input import AudioInput
from core.vad_runtime import VADRuntime


@dataclass(frozen=True)
class CommandRecorderConfig:
    """
    Settings used to capture one spoken Qronos command.

    speech_start_threshold:
        VAD probability required to begin recording speech.

    speech_continue_threshold:
        Lower probability used after speech has started. This hysteresis
        prevents quieter syllables from immediately ending the command.

    silence_seconds:
        Continuous trailing silence required to finish the command.

    max_duration_seconds:
        Maximum recorded command duration after speech begins.

    start_timeout_seconds:
        Maximum time to wait for speech to begin.

    pre_roll_seconds:
        Audio retained before speech detection so the first phoneme is not
        clipped.

    min_speech_seconds:
        Minimum amount of detected speech required before trailing silence
        is allowed to finish the command.
    """

    speech_start_threshold: float = 0.50
    speech_continue_threshold: float = 0.50

    silence_seconds: float = 2.0
    max_duration_seconds: float = 60.0
    start_timeout_seconds: float = 8.0

    pre_roll_seconds: float = 0.32
    min_speech_seconds: float = 0.24

    def __post_init__(self) -> None:
        if not (
            0.0
            < self.speech_start_threshold
            <= 1.0
        ):
            raise ValueError(
                "speech_start_threshold must "
                "be between 0 and 1."
            )

        if not (
            0.0
            < self.speech_continue_threshold
            <= 1.0
        ):
            raise ValueError(
                "speech_continue_threshold must "
                "be between 0 and 1."
            )

        if (
            self.speech_continue_threshold
            > self.speech_start_threshold
        ):
            raise ValueError(
                "speech_continue_threshold must "
                "not exceed speech_start_threshold."
            )

        if self.silence_seconds <= 0:
            raise ValueError(
                "silence_seconds must be "
                "greater than zero."
            )

        if self.max_duration_seconds <= 0:
            raise ValueError(
                "max_duration_seconds must be "
                "greater than zero."
            )

        if self.start_timeout_seconds <= 0:
            raise ValueError(
                "start_timeout_seconds must be "
                "greater than zero."
            )

        if self.pre_roll_seconds < 0:
            raise ValueError(
                "pre_roll_seconds must not "
                "be negative."
            )

        if self.min_speech_seconds <= 0:
            raise ValueError(
                "min_speech_seconds must be "
                "greater than zero."
            )


@dataclass(frozen=True)
class CommandRecordingResult:
    audio_path: Path
    duration_seconds: float
    speech_seconds: float
    stopped_by_silence: bool
    peak_speech_probability: float


class CommandRecorder:
    """
    Record one spoken command using a real VAD runtime.

    The recorder does not inspect raw signal energy. Speech decisions come
    from VAD probabilities, allowing the detector to distinguish speech from
    background noise more reliably than a fixed RMS threshold.
    """

    def __init__(
        self,
        audio_input: AudioInput,
        vad_runtime: VADRuntime,
        config: CommandRecorderConfig | None = None,
    ) -> None:
        self.audio_input = audio_input
        self.vad_runtime = vad_runtime
        self.config = (
            config
            if config is not None
            else CommandRecorderConfig()
        )

        if (
            self.audio_input.config.sample_width
            != 2
        ):
            raise ValueError(
                "CommandRecorder requires "
                "16-bit PCM audio."
            )

        if (
            self.audio_input.config.channels
            != 1
        ):
            raise ValueError(
                "CommandRecorder requires "
                "mono audio."
            )

        if (
            self.audio_input.config.sample_rate
            != self.vad_runtime.sample_rate
        ):
            raise ValueError(
                "AudioInput sample rate must "
                "match the VAD runtime sample rate."
            )

    def _seconds_to_frames(
        self,
        seconds: float,
    ) -> int:
        frame_duration = (
            self.audio_input.config.frame_size
            / self.audio_input.config.sample_rate
        )

        return max(
            1,
            math.ceil(
                seconds
                / frame_duration
            ),
        )

    @staticmethod
    def _highest_probability(
        probabilities: tuple[float, ...],
    ) -> float | None:
        if not probabilities:
            return None

        return max(
            probabilities
        )

    def record_to_file(
        self,
        output_path: str | Path,
    ) -> CommandRecordingResult:
        destination = Path(
            output_path
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        frame_duration = (
            self.audio_input.config.frame_size
            / self.audio_input.config.sample_rate
        )

        silence_limit_frames = (
            self._seconds_to_frames(
                self.config.silence_seconds
            )
        )

        maximum_frames = (
            self._seconds_to_frames(
                self.config.max_duration_seconds
            )
        )

        start_timeout_frames = (
            self._seconds_to_frames(
                self.config.start_timeout_seconds
            )
        )

        minimum_speech_frames = (
            self._seconds_to_frames(
                self.config.min_speech_seconds
            )
        )

        if (
            self.config.pre_roll_seconds
            == 0
        ):
            pre_roll_frames = 0

        else:
            pre_roll_frames = (
                self._seconds_to_frames(
                    self.config.pre_roll_seconds
                )
            )

        pre_roll: deque[bytes] = deque(
            maxlen=pre_roll_frames
        )

        recorded_frames: list[bytes] = []

        speech_started = False

        waiting_frames = 0
        captured_frames = 0

        active_speech_frames = 0
        consecutive_silent_frames = 0

        stopped_by_silence = False

        peak_speech_probability = 0.0

        was_running = (
            self.audio_input.is_running()
        )

        started_here = (
            not was_running
        )

        self.vad_runtime.reset()

        if started_here:
            self.audio_input.start()

        try:
            while True:
                frame = (
                    self.audio_input.read_frame()
                )

                probabilities = (
                    self.vad_runtime.process_pcm16(
                        frame
                    )
                )

                frame_probability = (
                    self._highest_probability(
                        probabilities
                    )
                )

                if (
                    frame_probability
                    is not None
                ):
                    peak_speech_probability = max(
                        peak_speech_probability,
                        frame_probability,
                    )

                if not speech_started:
                    waiting_frames += 1

                    should_start = (
                        frame_probability
                        is not None
                        and frame_probability
                        >= self.config.speech_start_threshold
                    )

                    if should_start:
                        speech_started = True

                        recorded_frames.extend(
                            pre_roll
                        )

                        recorded_frames.append(
                            frame
                        )

                        captured_frames = 1
                        active_speech_frames = 1
                        consecutive_silent_frames = 0

                    else:
                        if (
                            pre_roll_frames
                            > 0
                        ):
                            pre_roll.append(
                                frame
                            )

                        if (
                            waiting_frames
                            >= start_timeout_frames
                        ):
                            raise TimeoutError(
                                "No speech was detected "
                                "before the start timeout."
                            )

                    continue

                recorded_frames.append(
                    frame
                )

                captured_frames += 1

                if (
                    frame_probability
                    is None
                ):
                    pass

                elif (
                    frame_probability
                    >= self.config.speech_continue_threshold
                ):
                    active_speech_frames += 1
                    consecutive_silent_frames = 0

                else:
                    consecutive_silent_frames += 1

                if (
                    active_speech_frames
                    >= minimum_speech_frames
                    and consecutive_silent_frames
                    >= silence_limit_frames
                ):
                    stopped_by_silence = True
                    break

                if (
                    captured_frames
                    >= maximum_frames
                ):
                    break

        finally:
            if started_here:
                self.audio_input.stop()

        if not recorded_frames:
            raise RuntimeError(
                "Command recording produced "
                "no audio."
            )

        with wave.open(
            str(destination),
            "wb",
        ) as wav_file:
            wav_file.setnchannels(
                self.audio_input.config.channels
            )

            wav_file.setsampwidth(
                self.audio_input.config.sample_width
            )

            wav_file.setframerate(
                self.audio_input.config.sample_rate
            )

            wav_file.writeframes(
                b"".join(
                    recorded_frames
                )
            )

        return CommandRecordingResult(
            audio_path=destination,
            duration_seconds=(
                len(recorded_frames)
                * frame_duration
            ),
            speech_seconds=(
                active_speech_frames
                * frame_duration
            ),
            stopped_by_silence=(
                stopped_by_silence
            ),
            peak_speech_probability=(
                peak_speech_probability
            ),
        )