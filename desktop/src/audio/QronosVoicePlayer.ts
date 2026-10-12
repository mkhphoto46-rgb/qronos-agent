import {
  convertFileSrc,
  invoke,
} from "@tauri-apps/api/core";

import {
  isNativeAudioOutputDeviceId,
} from "./audioOutput";

export type VoicePlaybackSpeed =
  | "slow"
  | "normal"
  | "fast";

export type VoicePlaybackState =
  | "idle"
  | "loading"
  | "playing"
  | "paused"
  | "ended"
  | "stopped"
  | "error";

export type VoiceSpectrumFrame = {
  level: number;
  bands: number[];
};

type VoiceSpectrumSidecar = {
  frameSeconds: number;
  frames: VoiceSpectrumFrame[];
};

type VoicePlayerOptions = {
  onStateChange?: (
    state: VoicePlaybackState,
  ) => void;
  onSpectrumFrame?: (
    frame: VoiceSpectrumFrame,
  ) => void;
  onPlaybackStarted?: (
    startedAtMs: number,
  ) => void;
  onError?: (
    error: Error,
  ) => void;
};

const PLAYBACK_RATE: Record<
  VoicePlaybackSpeed,
  number
> = {
  slow: 0.85,
  normal: 1,
  fast: 1.25,
};

// Qronos may be moved between drives. Keep the runtime-temp boundary strict
// without pinning playback to one historical drive letter.
const ALLOWED_AUDIO_ROOT_PATTERN =
  /^[A-Za-z]:\\Project Qronos Agent\\runtime\\chatterbox\\temp\\/i;

const SPECTRUM_BANDS = 32;
const SPECTRUM_RETRY_MS = 80;
const SPECTRUM_RETRY_LIMIT = 80;

function normalizeWindowsPath(
  value: string,
): string {
  return value
    .trim()
    .replace(/\//g, "\\");
}

function asError(
  value: unknown,
): Error {
  return value instanceof Error
    ? value
    : new Error(
        String(value),
      );
}

function assertAllowedWavePath(
  inputPath: string,
): string {
  const normalized =
    normalizeWindowsPath(
      inputPath,
    );

  const lower =
    normalized.toLowerCase();

  const rootMatch =
    normalized.match(
      ALLOWED_AUDIO_ROOT_PATTERN,
    );

  if (!rootMatch) {
    throw new Error(
      "Qronos refused audio outside its runtime temp directory.",
    );
  }

  const root =
    rootMatch[0];

  const relative =
    normalized.slice(
      root.length,
    );

  const parts =
    relative
      .split("\\")
      .filter(Boolean);

  if (
    parts.length !== 1 ||
    parts.some(
      (part) =>
        part === "." ||
        part === "..",
    )
  ) {
    throw new Error(
      "Qronos refused an invalid runtime audio path.",
    );
  }

  if (!lower.endsWith(".wav")) {
    throw new Error(
      "Qronos voice playback accepts WAV files only.",
    );
  }

  return normalized;
}

function clamp01(
  value: number,
): number {
  return Math.max(
    0,
    Math.min(
      1,
      value,
    ),
  );
}

function silentSpectrumFrame():
  VoiceSpectrumFrame {
  return {
    level: 0,
    bands: Array.from(
      { length: SPECTRUM_BANDS },
      () => 0,
    ),
  };
}

function normalizeSpectrumFrame(
  input: unknown,
): VoiceSpectrumFrame {
  if (
    typeof input !== "object" ||
    input === null
  ) {
    return silentSpectrumFrame();
  }

  const candidate =
    input as {
      level?: unknown;
      bands?: unknown;
    };

  const level =
    typeof candidate.level === "number"
      ? clamp01(candidate.level)
      : 0;

  const bands =
    Array.isArray(candidate.bands)
      ? candidate.bands
          .slice(
            0,
            SPECTRUM_BANDS,
          )
          .map(
            (value) =>
              typeof value === "number"
                ? clamp01(value)
                : 0,
          )
      : [];

  while (
    bands.length <
    SPECTRUM_BANDS
  ) {
    bands.push(0);
  }

  return {
    level,
    bands,
  };
}

function sleep(
  milliseconds: number,
): Promise<void> {
  return new Promise(
    (resolve) => {
      window.setTimeout(
        resolve,
        milliseconds,
      );
    },
  );
}

export class QronosVoicePlayer {
  private audio:
    HTMLAudioElement | null =
      null;

  private state:
    VoicePlaybackState =
      "idle";

  private speed:
    VoicePlaybackSpeed =
      "normal";

  private outputDeviceId = "";

  private nativePlaybackActive = false;

  private nativePlaybackStartedAtMs = 0;

  private generation = 0;

  private queue: string[] = [];

  private animationFrameId:
    number | null =
      null;

  private spectrum:
    VoiceSpectrumSidecar | null =
      null;

  private readonly onStateChange?:
    VoicePlayerOptions[
      "onStateChange"
    ];

  private readonly onSpectrumFrame?:
    VoicePlayerOptions[
      "onSpectrumFrame"
    ];

  private readonly onPlaybackStarted?:
    VoicePlayerOptions[
      "onPlaybackStarted"
    ];

  private readonly onError?:
    VoicePlayerOptions[
      "onError"
    ];

  constructor(
    options: VoicePlayerOptions = {},
  ) {
    this.onStateChange =
      options.onStateChange;
    this.onSpectrumFrame =
      options.onSpectrumFrame;
    this.onPlaybackStarted =
      options.onPlaybackStarted;
    this.onError =
      options.onError;
  }

  getState():
    VoicePlaybackState {
    return this.state;
  }

  getSpeed():
    VoicePlaybackSpeed {
    return this.speed;
  }

  getPlaybackRate():
    number {
    return PLAYBACK_RATE[
      this.speed
    ];
  }

  getQueuedCount():
    number {
    return this.queue.length;
  }

  isActive():
    boolean {
    return (
      this.state === "loading" ||
      this.state === "playing" ||
      this.state === "paused" ||
      this.nativePlaybackActive ||
      this.queue.length > 0
    );
  }

  setSpeed(
    speed: VoicePlaybackSpeed,
  ): void {
    this.speed = speed;

    if (this.audio) {
      this.audio.playbackRate =
        PLAYBACK_RATE[speed];
    }
  }

  getOutputDeviceId():
    string {
    return this.outputDeviceId;
  }

  async setOutputDeviceId(
    deviceId: string,
  ): Promise<void> {
    // The system-default route stays in the WebView player. Explicit devices
    // are native PortAudio endpoints and are applied when each WAV starts.
    this.outputDeviceId =
      deviceId.trim();
  }

  async playPath(
    inputPath: string,
  ): Promise<void> {
    let path: string;

    try {
      path =
        assertAllowedWavePath(
          inputPath,
        );
    } catch (value) {
      // Path validation happens before any HTMLAudio/native playback object
      // exists. Surface it as a real player error so App.tsx can release the
      // matching runtime playback wait instead of deadlocking Wake.
      const error =
        asError(value);

      this.setState(
        "error",
      );
      this.onError?.(
        error,
      );

      throw error;
    }

    if (
      this.audio !== null ||
      this.nativePlaybackActive ||
      this.state === "loading" ||
      this.state === "playing" ||
      this.state === "paused"
    ) {
      this.queue.push(path);
      return;
    }

    await this.startPath(path);
  }

  pause(): void {
    if (
      !this.audio ||
      this.state !== "playing"
    ) {
      return;
    }

    this.audio.pause();
    this.stopSpectrumLoop();
    this.emitSilentSpectrum();
    this.setState(
      "paused",
    );
  }

  async resume():
    Promise<void> {
    if (
      !this.audio ||
      this.state !== "paused"
    ) {
      return;
    }

    try {
      await this.audio.play();
    } catch (value) {
      const error =
        asError(value);

      this.setState(
        "error",
      );
      this.onError?.(
        error,
      );
      throw error;
    }
  }

  stop(): void {
    ++this.generation;

    const hadNativePlayback =
      this.nativePlaybackActive;

    this.queue = [];
    this.stopSpectrumLoop();
    this.emitSilentSpectrum();
    this.spectrum = null;
    this.nativePlaybackActive = false;
    this.nativePlaybackStartedAtMs = 0;

    if (hadNativePlayback) {
      void invoke(
        "native_audio_output_stop",
      ).catch(
        (error) => {
          console.error(
            "[Qronos Voice Playback] native stop failed:",
            error,
          );
        },
      );
    }

    const audio =
      this.audio;

    if (!audio) {
      if (
        this.state !== "idle"
      ) {
        this.setState(
          "stopped",
        );
      }

      return;
    }

    audio.onplaying = null;
    audio.onpause = null;
    audio.onended = null;
    audio.onerror = null;

    audio.pause();

    try {
      audio.currentTime = 0;
    } catch {
      // Some media engines refuse seeking before metadata is loaded.
    }

    this.audio = null;

    this.setState(
      "stopped",
    );
  }

  dispose(): void {
    this.stop();
    this.audio = null;
    this.queue = [];
    this.spectrum = null;

    this.setState(
      "idle",
    );
  }

  private async startPath(
    path: string,
  ): Promise<void> {
    if (
      isNativeAudioOutputDeviceId(
        this.outputDeviceId,
      )
    ) {
      try {
        await this.startNativePath(
          path,
        );
        return;
      } catch (value) {
        const error =
          asError(value);

        // A selected endpoint may disappear between enumeration and playback.
        // Fail open for this response instead of making Qronos silent.
        this.nativePlaybackActive = false;
        this.nativePlaybackStartedAtMs = 0;
        this.stopSpectrumLoop();
        this.emitSilentSpectrum();
        this.onError?.(error);
      }
    }

    await this.startHtmlPath(
      path,
    );
  }

  private async startNativePath(
    path: string,
  ): Promise<void> {
    const generation =
      this.generation;

    this.audio = null;
    this.spectrum = null;
    this.nativePlaybackActive = true;
    this.nativePlaybackStartedAtMs =
      performance.now();

    this.setState(
      "loading",
    );

    void this.loadSpectrumSidecarWithRetry(
      path,
      generation,
    );

    this.setState(
      "playing",
    );

    this.onPlaybackStarted?.(
      this.nativePlaybackStartedAtMs,
    );

    this.startSpectrumLoop(
      generation,
    );

    try {
      await invoke(
        "native_audio_output_play",
        {
          path,
          deviceId:
            this.outputDeviceId,
        },
      );
    } catch (value) {
      if (
        generation !==
        this.generation
      ) {
        return;
      }

      this.nativePlaybackActive = false;
      this.nativePlaybackStartedAtMs = 0;
      throw value;
    }

    if (
      generation !==
      this.generation
    ) {
      return;
    }

    this.nativePlaybackActive = false;
    this.nativePlaybackStartedAtMs = 0;
    this.stopSpectrumLoop();
    this.emitSilentSpectrum();
    this.spectrum = null;

    await this.startNextQueuedPath(
      generation,
    );
  }

  private async startHtmlPath(
    path: string,
  ): Promise<void> {
    const generation =
      this.generation;

    const audio =
      new Audio(
        convertFileSrc(path),
      );

    audio.preload = "auto";
    audio.playbackRate =
      PLAYBACK_RATE[
        this.speed
      ];

    this.audio = audio;
    this.spectrum = null;

    this.setState(
      "loading",
    );

    void this.loadSpectrumSidecarWithRetry(
      path,
      generation,
    );

    audio.onplaying = () => {
      if (
        generation !==
        this.generation ||
        this.audio !== audio
      ) {
        return;
      }

      this.setState(
        "playing",
      );

      this.onPlaybackStarted?.(
        performance.now(),
      );

      this.startSpectrumLoop(
        generation,
      );
    };

    audio.onpause = () => {
      if (
        generation !==
          this.generation ||
        this.audio !== audio ||
        audio.ended
      ) {
        return;
      }

      this.stopSpectrumLoop();

      if (
        this.state !==
        "stopped"
      ) {
        this.setState(
          "paused",
        );
        this.emitSilentSpectrum();
      }
    };

    audio.onended = () => {
      if (
        generation !==
          this.generation ||
        this.audio !== audio
      ) {
        return;
      }

      this.stopSpectrumLoop();
      this.emitSilentSpectrum();

      audio.onplaying = null;
      audio.onpause = null;
      audio.onended = null;
      audio.onerror = null;

      this.audio = null;
      this.spectrum = null;

      void this.startNextQueuedPath(
        generation,
      );
    };

    audio.onerror = () => {
      if (
        generation !==
          this.generation ||
        this.audio !== audio
      ) {
        return;
      }

      const error =
        new Error(
          "Qronos could not decode or play the generated WAV file.",
        );

      this.stopSpectrumLoop();
      this.emitSilentSpectrum();

      audio.onplaying = null;
      audio.onpause = null;
      audio.onended = null;
      audio.onerror = null;

      this.audio = null;
      this.spectrum = null;

      this.onError?.(
        error,
      );

      void this.startNextQueuedPath(
        generation,
      );
    };

    try {
      await audio.play();
    } catch (value) {
      if (
        generation !==
          this.generation ||
        this.audio !== audio
      ) {
        return;
      }

      const error =
        asError(value);

      this.stopSpectrumLoop();
      this.emitSilentSpectrum();

      this.audio = null;
      this.spectrum = null;

      this.setState(
        "error",
      );
      this.onError?.(
        error,
      );

      throw error;
    }
  }

  private async startNextQueuedPath(
    generation: number,
  ): Promise<void> {
    if (
      generation !==
      this.generation
    ) {
      return;
    }

    const next =
      this.queue.shift();

    if (!next) {
      this.setState(
        "ended",
      );
      return;
    }

    try {
      await this.startPath(next);
    } catch {
      if (
        generation ===
        this.generation
      ) {
        await this.startNextQueuedPath(
          generation,
        );
      }
    }
  }

  private async loadSpectrumSidecarWithRetry(
    wavePath: string,
    generation: number,
  ): Promise<void> {
    const sidecarPath =
      wavePath.replace(
        /\.wav$/i,
        ".spectrum.json",
      );

    const sidecarUrl =
      convertFileSrc(
        sidecarPath,
      );

    for (
      let attempt = 0;
      attempt <
      SPECTRUM_RETRY_LIMIT;
      attempt += 1
    ) {
      if (
        generation !==
        this.generation
      ) {
        return;
      }

      try {
        const response =
          await fetch(
            sidecarUrl,
            {
              cache: "no-store",
            },
          );

        if (response.ok) {
          const parsed =
            await response.json() as {
              frameSeconds?: unknown;
              frames?: unknown;
            };

          if (
            generation !==
            this.generation
          ) {
            return;
          }

          if (
            typeof parsed.frameSeconds ===
              "number" &&
            parsed.frameSeconds > 0 &&
            Array.isArray(
              parsed.frames,
            )
          ) {
            this.spectrum = {
              frameSeconds:
                parsed.frameSeconds,
              frames:
                parsed.frames.map(
                  normalizeSpectrumFrame,
                ),
            };

            return;
          }
        }
      } catch {
        // Sidecar may not exist yet. Retry without affecting playback.
      }

      await sleep(
        SPECTRUM_RETRY_MS,
      );
    }
  }

  private startSpectrumLoop(
    generation: number,
  ): void {
    this.stopSpectrumLoop();

    const tick = () => {
      if (
        generation !==
          this.generation ||
        this.state !== "playing"
      ) {
        return;
      }

      this.emitSpectrumFrame();

      this.animationFrameId =
        window.requestAnimationFrame(
          tick,
        );
    };

    this.animationFrameId =
      window.requestAnimationFrame(
        tick,
      );
  }

  private stopSpectrumLoop():
    void {
    if (
      this.animationFrameId !==
      null
    ) {
      window.cancelAnimationFrame(
        this.animationFrameId,
      );

      this.animationFrameId =
        null;
    }
  }

  private playbackPositionSeconds():
    number | null {
    if (this.audio) {
      return this.audio.currentTime;
    }

    if (
      this.nativePlaybackActive &&
      this.nativePlaybackStartedAtMs > 0
    ) {
      return Math.max(
        0,
        (
          performance.now() -
          this.nativePlaybackStartedAtMs
        ) / 1000,
      );
    }

    return null;
  }

  private emitSpectrumFrame():
    void {
    if (
      !this.spectrum ||
      this.spectrum.frames.length ===
        0
    ) {
      return;
    }

    const position =
      this.playbackPositionSeconds();

    if (position === null) {
      return;
    }

    const index =
      Math.max(
        0,
        Math.min(
          this.spectrum.frames.length -
            1,
          Math.floor(
            position /
              this.spectrum.frameSeconds,
          ),
        ),
      );

    this.onSpectrumFrame?.(
      this.spectrum.frames[
        index
      ],
    );
  }

  private emitSilentSpectrum():
    void {
    this.onSpectrumFrame?.(
      silentSpectrumFrame(),
    );
  }

  private setState(
    state: VoicePlaybackState,
  ): void {
    this.state = state;

    this.onStateChange?.(
      state,
    );
  }
}
