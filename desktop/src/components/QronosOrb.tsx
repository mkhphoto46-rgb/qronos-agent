import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import type { OrbState } from "./OrbState";

type Particle = {
  latitude: number;
  longitude: number;
  radialNoise: number;
  sizeNoise: number;
  speedNoise: number;
  centerNoise: number;
  brightnessNoise: number;
};

type RoamingParticle = {
  angle: number;
  phase: number;
  lane: number;
  size: number;
  alpha: number;
  speedNoise: number;
  chaosNoise: number;
};

type AmbientParticle = {
  angle: number;
  radiusFactor: number;
  speed: number;
  size: number;
  alpha: number;
  phase: number;
};

type BurstSpark = {
  startTime: number;
  duration: number;
  angle: number;
  distance: number;
  size: number;
  brightness: number;
  tangent: number;
  far: boolean;
  micro: boolean;
};

type CachedRoamingPoint = {
  x: number;
  y: number;
  size: number;
  alpha: number;
  glow: number;
};

type VisualState = {
  baseLuminosity: number;

  rotationSpeed: number;
  surfaceSpeed: number;

  breathSpeed: number;
  breathScale: number;
  breathLight: number;

  heartbeatSpeed: number;
  heartbeatScale: number;
  heartbeatLight: number;

  waveStrength: number;
  asymmetry: number;

  shell2Strength: number;
  shell2Drift: number;
  shell2Width: number;

  shell3Strength: number;
  shell3Drift: number;
  shell3Width: number;

  foldStrength: number;
  foldCompression: number;
  foldBrightness: number;
  foldSpeed: number;

  escapeStrength: number;
  escapeSpeed: number;

  chaosStrength: number;
  chaosSpeed: number;

  violetStrength: number;
  violetWidth: number;
  violetTwist: number;

  particleGlow: number;
  environmentGlow: number;
  ridgeGlow: number;

  burstRate: number;
  burstEnergy: number;

  microEruptionRate: number;
  microEruptionEnergy: number;
};

type StatePresence = {
  idle: number;
  listening: number;
  thinking: number;
  responding: number;
};

type QronosOrbProps = {
  size?: number;
  state?: OrbState;
};

const MAX_DPR = 1.5;

const VISUAL_STATES: Record<OrbState, VisualState> = {
  idle: {
    baseLuminosity: 0.56,

    rotationSpeed: 0.11,
    surfaceSpeed: 0.36,

    breathSpeed: 0.72,
    breathScale: 0.011,
    breathLight: 0.025,

    heartbeatSpeed: 1.55,
    heartbeatScale: 0.01,
    heartbeatLight: 0.032,

    waveStrength: 0.82,
    asymmetry: 0.44,

    shell2Strength: 0.26,
    shell2Drift: 0.018,
    shell2Width: 0.5,

    shell3Strength: 0.18,
    shell3Drift: 0.024,
    shell3Width: 0.34,

    foldStrength: 0.2,
    foldCompression: 0.22,
    foldBrightness: 0.16,
    foldSpeed: 0.22,

    escapeStrength: 0.07,
    escapeSpeed: 0.14,

    chaosStrength: 0.06,
    chaosSpeed: 0.12,

    violetStrength: 0.1,
    violetWidth: 0.18,
    violetTwist: 0.12,

    particleGlow: 0.12,
    environmentGlow: 0.06,
    ridgeGlow: 0.12,

    burstRate: 0.022,
    burstEnergy: 0.1,

    microEruptionRate: 0.015,
    microEruptionEnergy: 0.08,
  },

  listening: {
    baseLuminosity: 0.75,

    rotationSpeed: 0.19,
    surfaceSpeed: 0.84,

    breathSpeed: 0.98,
    breathScale: 0.019,
    breathLight: 0.055,

    heartbeatSpeed: 2.05,
    heartbeatScale: 0.017,
    heartbeatLight: 0.072,

    waveStrength: 1.28,
    asymmetry: 0.72,

    shell2Strength: 0.72,
    shell2Drift: 0.038,
    shell2Width: 0.58,

    shell3Strength: 0.54,
    shell3Drift: 0.048,
    shell3Width: 0.4,

    foldStrength: 0.96,
    foldCompression: 0.94,
    foldBrightness: 0.78,
    foldSpeed: 0.62,

    escapeStrength: 0.56,
    escapeSpeed: 0.23,

    chaosStrength: 0.48,
    chaosSpeed: 0.22,

    violetStrength: 0.48,
    violetWidth: 0.28,
    violetTwist: 0.34,

    particleGlow: 0.34,
    environmentGlow: 0.25,
    ridgeGlow: 0.48,

    burstRate: 0.52,
    burstEnergy: 0.76,

    microEruptionRate: 0.25,
    microEruptionEnergy: 0.38,
  },

  thinking: {
    baseLuminosity: 0.8,

    rotationSpeed: 0.31,
    surfaceSpeed: 1.22,

    breathSpeed: 1.05,
    breathScale: 0.022,
    breathLight: 0.062,

    heartbeatSpeed: 2.25,
    heartbeatScale: 0.021,
    heartbeatLight: 0.082,

    waveStrength: 1.62,
    asymmetry: 1,

    shell2Strength: 1,
    shell2Drift: 0.06,
    shell2Width: 0.64,

    shell3Strength: 0.92,
    shell3Drift: 0.07,
    shell3Width: 0.44,

    foldStrength: 1.42,
    foldCompression: 1.34,
    foldBrightness: 1.12,
    foldSpeed: 0.9,

    escapeStrength: 1,
    escapeSpeed: 0.31,

    chaosStrength: 1,
    chaosSpeed: 0.32,

    violetStrength: 0.82,
    violetWidth: 0.36,
    violetTwist: 0.62,

    particleGlow: 0.56,
    environmentGlow: 0.46,
    ridgeGlow: 0.84,

    burstRate: 1.08,
    burstEnergy: 1,

    microEruptionRate: 0.95,
    microEruptionEnergy: 1,
  },

  responding: {
    baseLuminosity: 0.69,

    rotationSpeed: 0.235,
    surfaceSpeed: 0.95,

    breathSpeed: 1.02,
    breathScale: 0.02,
    breathLight: 0.05,

    heartbeatSpeed: 2.15,
    heartbeatScale: 0.019,
    heartbeatLight: 0.064,

    waveStrength: 1.38,
    asymmetry: 0.8,

    shell2Strength: 0.82,
    shell2Drift: 0.05,
    shell2Width: 0.6,

    shell3Strength: 0.68,
    shell3Drift: 0.058,
    shell3Width: 0.42,

    foldStrength: 0.82,
    foldCompression: 0.78,
    foldBrightness: 0.68,
    foldSpeed: 0.68,

    escapeStrength: 1,
    escapeSpeed: 0.31,

    chaosStrength: 1,
    chaosSpeed: 0.32,

    violetStrength: 0.82,
    violetWidth: 0.36,
    violetTwist: 0.62,

    particleGlow: 0.5,
    environmentGlow: 0.42,
    ridgeGlow: 0.72,

    burstRate: 0.54,
    burstEnergy: 0.78,

    microEruptionRate: 0.54,
    microEruptionEnergy: 0.66,
  },
};

function lerp(
  current: number,
  target: number,
  amount: number,
) {
  return current + (target - current) * amount;
}

function clamp01(value: number) {
  return Math.max(0, Math.min(1, value));
}

function QronosOrb({
  size = 460,
  state = "idle",
}: QronosOrbProps) {
  const coreCanvasRef =
    useRef<HTMLCanvasElement | null>(null);

  const backEffectsCanvasRef =
    useRef<HTMLCanvasElement | null>(null);

  const frontEffectsCanvasRef =
    useRef<HTMLCanvasElement | null>(null);

  const stateRef =
    useRef<OrbState>(state);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    const coreCanvas =
      coreCanvasRef.current;

    const backCanvas =
      backEffectsCanvasRef.current;

    const frontCanvas =
      frontEffectsCanvasRef.current;

    if (
      !coreCanvas ||
      !backCanvas ||
      !frontCanvas
    ) {
      return;
    }

    const coreCtx =
      coreCanvas.getContext("2d", {
        alpha: true,
        desynchronized: true,
      });

    const backCtx =
      backCanvas.getContext("2d", {
        alpha: true,
        desynchronized: true,
      });

    const frontCtx =
      frontCanvas.getContext("2d", {
        alpha: true,
        desynchronized: true,
      });

    if (
      !coreCtx ||
      !backCtx ||
      !frontCtx
    ) {
      return;
    }

    let dpr = Math.min(
      window.devicePixelRatio || 1,
      MAX_DPR,
    );

    let renderSize = size;

    let viewportWidth =
      window.innerWidth;

    let viewportHeight =
      window.innerHeight;

    const createGlowSprite = (
      inner: string,
      middle: string,
      outer: string,
    ) => {
      const sprite =
        document.createElement("canvas");

      sprite.width = 64;
      sprite.height = 64;

      const spriteCtx =
        sprite.getContext("2d");

      if (!spriteCtx) {
        return sprite;
      }

      const gradient =
        spriteCtx.createRadialGradient(
          32,
          32,
          0,
          32,
          32,
          32,
        );

      gradient.addColorStop(0, inner);
      gradient.addColorStop(
        0.16,
        middle,
      );
      gradient.addColorStop(
        0.52,
        outer,
      );
      gradient.addColorStop(
        1,
        "rgba(0,0,0,0)",
      );

      spriteCtx.fillStyle =
        gradient;

      spriteCtx.fillRect(
        0,
        0,
        64,
        64,
      );

      return sprite;
    };

    const cyanGlowSprite =
      createGlowSprite(
        "rgba(220,252,255,0.95)",
        "rgba(98,230,255,0.42)",
        "rgba(60,190,255,0.07)",
      );

    const violetGlowSprite =
      createGlowSprite(
        "rgba(238,226,255,0.92)",
        "rgba(151,101,255,0.42)",
        "rgba(105,74,255,0.06)",
      );

    const drawGlowSprite = (
      ctx: CanvasRenderingContext2D,
      sprite: HTMLCanvasElement,
      x: number,
      y: number,
      radius: number,
      strength: number,
    ) => {
      if (strength < 0.02) {
        return;
      }

      const diameter =
        Math.max(
          5,
          radius *
            (7 + strength * 7),
        );

      const previousAlpha =
        ctx.globalAlpha;

      ctx.globalAlpha =
        Math.min(
          0.66,
          0.13 +
            strength * 0.48,
        );

      ctx.drawImage(
        sprite,
        x - diameter / 2,
        y - diameter / 2,
        diameter,
        diameter,
      );

      ctx.globalAlpha =
        previousAlpha;
    };

    const resizeCoreCanvas = () => {
      const parent =
        coreCanvas.parentElement;

      if (!parent) {
        return;
      }

      const logicalWidth =
        parent.clientWidth;

      const logicalHeight =
        parent.clientHeight;

      renderSize =
        Math.max(
          40,
          Math.min(
            size,
            logicalWidth,
            logicalHeight,
          ),
        );

      dpr =
        Math.min(
          window.devicePixelRatio || 1,
          MAX_DPR,
        );

      coreCanvas.width =
        Math.round(
          renderSize * dpr,
        );

      coreCanvas.height =
        Math.round(
          renderSize * dpr,
        );

      coreCanvas.style.width =
        `${renderSize}px`;

      coreCanvas.style.height =
        `${renderSize}px`;

      coreCtx.setTransform(
        dpr,
        0,
        0,
        dpr,
        0,
        0,
      );
    };

    const resizeEffectsCanvas = () => {
      viewportWidth =
        window.innerWidth;

      viewportHeight =
        window.innerHeight;

      dpr =
        Math.min(
          window.devicePixelRatio || 1,
          MAX_DPR,
        );

      const targets = [
        {
          canvas: backCanvas,
          ctx: backCtx,
        },
        {
          canvas: frontCanvas,
          ctx: frontCtx,
        },
      ];

      for (const target of targets) {
        target.canvas.width =
          Math.round(
            viewportWidth * dpr,
          );

        target.canvas.height =
          Math.round(
            viewportHeight * dpr,
          );

        target.canvas.style.width =
          `${viewportWidth}px`;

        target.canvas.style.height =
          `${viewportHeight}px`;

        target.ctx.setTransform(
          dpr,
          0,
          0,
          dpr,
          0,
          0,
        );
      }
    };

    resizeCoreCanvas();
    resizeEffectsCanvas();

    const resizeObserver =
      new ResizeObserver(() => {
        resizeCoreCanvas();
      });

    if (
      coreCanvas.parentElement
    ) {
      resizeObserver.observe(
        coreCanvas.parentElement,
      );
    }

    const onWindowResize = () => {
      resizeEffectsCanvas();
      resizeCoreCanvas();
    };

    window.addEventListener(
      "resize",
      onWindowResize,
    );

    const baseParticles: Particle[] =
      [];

    const shell2Particles: Particle[] =
      [];

    const shell3Particles: Particle[] =
      [];

    const ambientParticles: AmbientParticle[] =
      [];

    const backRoamingParticles: RoamingParticle[] =
      [];

    const frontRoamingParticles: RoamingParticle[] =
      [];

    const burstSparks: BurstSpark[] =
      [];

    const latitudeBands = 60;
    const equatorBands = 220;

    for (
      let latIndex = 0;
      latIndex < latitudeBands;
      latIndex += 1
    ) {
      const latitude =
        -Math.PI / 2 +
        ((latIndex + 0.5) /
          latitudeBands) *
          Math.PI;

      const density =
        Math.max(
          0.08,
          Math.cos(latitude),
        );

      const longitudeBands =
        Math.max(
          14,
          Math.round(
            equatorBands * density,
          ),
        );

      for (
        let lonIndex = 0;
        lonIndex < longitudeBands;
        lonIndex += 1
      ) {
        const longitude =
          (lonIndex /
            longitudeBands) *
          Math.PI *
          2;

        const seed =
          Math.sin(
            latIndex * 12.9898 +
              lonIndex * 78.233,
          ) *
          43758.5453;

        const seed2 =
          Math.sin(
            latIndex * 31.731 +
              lonIndex * 17.117,
          ) *
          21942.338;

        const seed3 =
          Math.sin(
            latIndex * 9.137 +
              lonIndex * 47.531,
          ) *
          17421.551;

        baseParticles.push({
          latitude,
          longitude,

          radialNoise:
            Math.sin(seed) * 4.4,

          sizeNoise:
            0.14 +
            ((Math.cos(
              seed * 0.71,
            ) +
              1) /
              2) *
              0.34,

          speedNoise:
            0.75 +
            ((Math.sin(
              seed * 0.41,
            ) +
              1) /
              2) *
              0.62,

          centerNoise:
            (Math.sin(seed2) +
              1) /
            2,

          brightnessNoise:
            0.9 +
            ((Math.sin(seed3) +
              1) /
              2) *
              0.34,
        });
      }
    }

    const shell2Count = 2300;

    for (
      let index = 0;
      index < shell2Count;
      index += 1
    ) {
      const u =
        (index + 0.5) /
        shell2Count;

      const latitude =
        Math.asin(
          2 * u - 1,
        );

      const longitude =
        (index *
          2.399963229728653) %
        (Math.PI * 2);

      const seed =
        Math.sin(
          index * 18.771,
        ) *
        48127.291;

      const seed2 =
        Math.sin(
          index * 42.113,
        ) *
        27817.419;

      shell2Particles.push({
        latitude,
        longitude,

        radialNoise:
          Math.sin(seed) * 3.6,

        sizeNoise:
          0.12 +
          ((Math.cos(seed) +
            1) /
            2) *
            0.34,

        speedNoise:
          0.8 +
          ((Math.sin(seed2) +
            1) /
            2) *
            0.5,

        centerNoise:
          (Math.sin(
            seed * 0.63,
          ) +
            1) /
          2,

        brightnessNoise:
          0.9 +
          ((Math.cos(
            seed2 * 0.8,
          ) +
            1) /
            2) *
            0.28,
      });
    }

    const shell3Count = 1450;

    for (
      let index = 0;
      index < shell3Count;
      index += 1
    ) {
      const t =
        index /
        shell3Count;

      const latitude =
        -Math.PI / 2 +
        t * Math.PI;

      const longitude =
        (
          t *
            Math.PI *
            8.5 +
          Math.sin(
            t *
              Math.PI *
              7,
          ) *
            0.55
        ) %
        (Math.PI * 2);

      const seed =
        Math.sin(
          index * 27.117,
        ) *
        17231.713;

      const seed2 =
        Math.sin(
          index * 61.417,
        ) *
        19191.173;

      shell3Particles.push({
        latitude:
          latitude +
          Math.sin(
            t *
              Math.PI *
              13,
          ) *
            0.045,

        longitude:
          longitude +
          Math.sin(seed) *
            0.03,

        radialNoise:
          Math.sin(seed) * 3.2,

        sizeNoise:
          0.13 +
          ((Math.cos(
            seed * 0.8,
          ) +
            1) /
            2) *
            0.36,

        speedNoise:
          0.9 +
          ((Math.sin(seed2) +
            1) /
            2) *
            0.46,

        centerNoise:
          (Math.sin(
            seed * 0.73,
          ) +
            1) /
          2,

        brightnessNoise:
          0.96 +
          ((Math.cos(
            seed2 * 0.71,
          ) +
            1) /
            2) *
            0.32,
      });
    }

    for (
      let index = 0;
      index < 260;
      index += 1
    ) {
      const seedA =
        Math.sin(
          index * 15.17,
        ) *
        9173.21;

      const seedB =
        Math.sin(
          index * 61.31,
        ) *
        7711.42;

      ambientParticles.push({
        angle:
          (index / 260) *
          Math.PI *
          2,

        radiusFactor:
          1.02 +
          ((Math.sin(seedA) +
            1) /
            2) *
            0.46,

        speed:
          0.016 +
          ((Math.cos(seedB) +
            1) /
            2) *
            0.085,

        size:
          0.09 +
          ((Math.sin(seedB) +
            1) /
            2) *
            0.32,

        alpha:
          0.025 +
          ((Math.cos(seedA) +
            1) /
            2) *
            0.17,

        phase:
          ((Math.sin(
            index * 33.71,
          ) +
            1) /
            2) *
          Math.PI *
          2,
      });
    }

    for (
      let index = 0;
      index < 760;
      index += 1
    ) {
      const seed =
        Math.sin(
          index * 17.771,
        ) *
        31731.137;

      const seed2 =
        Math.sin(
          index * 41.137,
        ) *
        21137.713;

      const seed3 =
        Math.sin(
          index * 71.731,
        ) *
        14717.319;

      backRoamingParticles.push({
        angle:
          (index / 760) *
          Math.PI *
          2,

        phase:
          ((Math.sin(seed) +
            1) /
            2) *
          Math.PI *
          2,

        lane:
          (Math.sin(seed2) +
            1) /
          2,

        size:
          0.14 +
          ((Math.cos(seed) +
            1) /
            2) *
            0.4,

        alpha:
          0.11 +
          ((Math.sin(seed2) +
            1) /
            2) *
            0.32,

        speedNoise:
          0.84 +
          ((Math.cos(seed2) +
            1) /
            2) *
            0.32,

        chaosNoise:
          (Math.sin(seed3) +
            1) /
          2,
      });
    }

    for (
      let index = 0;
      index < 980;
      index += 1
    ) {
      const seed =
        Math.sin(
          index * 23.117,
        ) *
        27131.731;

      const seed2 =
        Math.sin(
          index * 57.713,
        ) *
        39117.331;

      const seed3 =
        Math.sin(
          index * 91.331,
        ) *
        23197.173;

      frontRoamingParticles.push({
        angle:
          (index / 980) *
          Math.PI *
          2,

        phase:
          ((Math.sin(seed) +
            1) /
            2) *
          Math.PI *
          2,

        lane:
          (Math.cos(seed2) +
            1) /
          2,

        size:
          0.16 +
          ((Math.cos(seed) +
            1) /
            2) *
            0.46,

        alpha:
          0.13 +
          ((Math.sin(seed2) +
            1) /
            2) *
            0.38,

        speedNoise:
          0.82 +
          ((Math.cos(seed2) +
            1) /
            2) *
            0.36,

        chaosNoise:
          (Math.sin(seed3) +
            1) /
          2,
      });
    }

    const cachedBackPoints:
      CachedRoamingPoint[] =
      backRoamingParticles.map(
        () => ({
          x: 0,
          y: 0,
          size: 0,
          alpha: 0,
          glow: 0,
        }),
      );

    const cachedFrontPoints:
      CachedRoamingPoint[] =
      frontRoamingParticles.map(
        () => ({
          x: 0,
          y: 0,
          size: 0,
          alpha: 0,
          glow: 0,
        }),
      );

    const visual: VisualState = {
      ...VISUAL_STATES[
        stateRef.current
      ],
    };

    const presence: StatePresence = {
      idle:
        stateRef.current ===
        "idle"
          ? 1
          : 0,

      listening:
        stateRef.current ===
        "listening"
          ? 1
          : 0,

      thinking:
        stateRef.current ===
        "thinking"
          ? 1
          : 0,

      responding:
        stateRef.current ===
        "responding"
          ? 1
          : 0,
    };

    let animationFrame = 0;

    let previousTimestamp = 0;

    let bodyRotationPhase = 0;

    let shell2DriftPhase = 0;
    let shell3DriftPhase = 0;

    let transitionSpinPhase = 0;

    let surfacePhase = 0;

    let foldPhase = 0;
    let foldSecondaryPhase = 0;

    let escapePhase = 0;
    let escapeSecondaryPhase = 0;

    let chaosPhase = 0;
    let chaosSecondaryPhase = 0;

    let violetPhase = 0;
    let violetSecondaryPhase = 0;

    let breathPhase = 0;
    let heartbeatPhase = 0;

    let lastState =
      stateRef.current;

    let transitionAge = 999;

    const transitionDuration =
      2.2;

    let burstCharge = 0;

    let burstThreshold =
      0.8 +
      Math.random() * 0.35;

    /*
     * NEW
     * Independent micro-eruption budget.
     */
    let microEruptionCharge = 0;

    let microEruptionThreshold =
      0.7 +
      Math.random() * 0.55;

    const projectParticle = (
      particle: Particle,
      radius: number,
      longitudeOffset: number,
      tiltX: number,
      tiltZ: number,
      centerX: number,
      centerY: number,
      projectionSize: number,
    ) => {
      const lat =
        particle.latitude;

      const lon =
        particle.longitude +
        longitudeOffset;

      let x =
        Math.cos(lat) *
        Math.cos(lon) *
        radius;

      let y =
        Math.sin(lat) *
        radius;

      let z =
        Math.cos(lat) *
        Math.sin(lon) *
        radius;

      const cosX =
        Math.cos(tiltX);

      const sinX =
        Math.sin(tiltX);

      const y1 =
        y * cosX -
        z * sinX;

      const z1 =
        y * sinX +
        z * cosX;

      y = y1;
      z = z1;

      const cosZ =
        Math.cos(tiltZ);

      const sinZ =
        Math.sin(tiltZ);

      const x2 =
        x * cosZ -
        y * sinZ;

      const y2 =
        x * sinZ +
        y * cosZ;

      x = x2;
      y = y2;

      const perspective =
        1 +
        z /
          (
            projectionSize *
            1.95
          );

      return {
        x:
          centerX +
          x * perspective,

        y:
          centerY +
          y *
            0.94 *
            perspective,

        z,
      };
    };

    const updateRoamingCache = (
      particles: RoamingParticle[],
      cache: CachedRoamingPoint[],
      front: boolean,
      centerX: number,
      centerY: number,
      baseRadius: number,
    ) => {
      const shortestSide =
        Math.min(
          viewportWidth,
          viewportHeight,
        );

      const maximumReach =
        Math.min(
          shortestSide * 0.48,
          440,
        );

      const cleanReach =
        maximumReach *
        visual.escapeStrength;

      const wildReach =
        maximumReach *
        visual.chaosStrength;

      const direction =
        front ? 1 : -1;

      const cleanRotation =
        bodyRotationPhase +
        transitionSpinPhase +
        escapePhase *
          (
            front
              ? 0.25
              : -0.19
          );

      for (
        let index = 0;
        index < particles.length;
        index += 1
      ) {
        const particle =
          particles[index];

        const point =
          cache[index];

        const carrier =
          (
            Math.sin(
              particle.phase +
              escapePhase *
                particle.speedNoise *
                (
                  front
                    ? 1.9
                    : 1.55
                ) +
              particle.angle *
                (
                  front
                    ? 1.65
                    : 1.25
                ),
            ) +
            1
          ) /
          2;

        const escapeGate =
          Math.pow(
            carrier,
            front
              ? 2.1
              : 2.45,
          );

        const cleanWave =
          Math.sin(
            particle.angle *
              (
                front
                  ? 4.8
                  : 6.2
              ) +
              escapeSecondaryPhase *
                (
                  front
                    ? 1.45
                    : -1.25
                ) +
              particle.phase,
          );

        const wildCarrierA =
          (
            Math.sin(
              particle.angle *
                2.7 -
              chaosPhase *
                particle.speedNoise *
                1.75 +
              particle.phase *
                1.35,
            ) +
            1
          ) /
          2;

        const wildCarrierB =
          (
            Math.sin(
              particle.angle *
                5.4 +
              chaosSecondaryPhase *
                1.28 -
              particle.phase *
                0.72,
            ) +
            1
          ) /
          2;

        const wildGate =
          Math.pow(
            wildCarrierA,
            2.6,
          ) *
          (
            0.36 +
            wildCarrierB *
              0.64
          );

        const chaosLane =
          (
            0.28 +
            particle.lane *
              0.72
          ) *
          (
            0.68 +
            particle.chaosNoise *
              0.52
          );

        const wildRadialWave =
          wildGate *
          wildReach *
          chaosLane;

        const wildLateral =
          Math.sin(
            particle.angle *
              3.7 +
              chaosPhase *
                (
                  front
                    ? 1.45
                    : -1.2
                ) +
              particle.phase *
                1.8,
          ) *
          visual.chaosStrength *
          (
            0.08 +
            wildGate *
              0.34
          );

        const dirtyWobble =
          Math.sin(
            particle.angle *
              8.3 -
              chaosSecondaryPhase *
                1.1 +
              particle.phase,
          ) *
          visual.chaosStrength *
          (
            6 +
            wildGate * 22
          );

        const cleanAngle =
          particle.angle +
          cleanRotation +
          cleanWave *
            (
              0.08 +
              visual.escapeStrength *
                0.27
            ) *
            direction;

        const cleanRadius =
          baseRadius *
            (
              front
                ? 1.025
                : 1.01
            ) +
          escapeGate *
            cleanReach *
            (
              0.32 +
              particle.lane *
                0.68
            );

        const angle =
          cleanAngle +
          wildLateral *
            direction;

        const radius =
          cleanRadius +
          wildRadialWave +
          dirtyWobble;

        const flowX =
          Math.sin(
            angle * 1.7 +
              chaosPhase * 0.7 +
              particle.phase,
          ) *
          visual.chaosStrength *
          wildGate *
          34;

        const flowY =
          Math.cos(
            angle * 2.15 -
              chaosSecondaryPhase *
                0.65 +
              particle.phase,
          ) *
          visual.chaosStrength *
          wildGate *
          28;

        point.x =
          centerX +
          Math.cos(angle) *
            radius +
          flowX;

        point.y =
          centerY +
          Math.sin(angle) *
            radius *
            (
              front
                ? 0.83
                : 0.9
            ) +
          cleanWave *
            visual.escapeStrength *
            18 +
          flowY;

        const distanceFade =
          1 -
          Math.max(
            escapeGate,
            wildGate,
          ) *
            0.32;

        const baseActivity =
          (
            front
              ? 0.64
              : 0.48
          ) *
          visual.escapeStrength +
          0.06;

        const chaosActivity =
          visual.chaosStrength *
          wildGate *
          (
            front
              ? 0.32
              : 0.25
          );

        point.alpha =
          particle.alpha *
          (
            baseActivity +
            chaosActivity
          ) *
          distanceFade;

        point.size =
          particle.size *
          (
            front
              ? 1.03
              : 0.84
          ) *
          (
            0.78 +
            escapeGate * 0.36 +
            wildGate * 0.42
          );

        if (
          particle.chaosNoise >
            0.93 &&
          point.alpha >
            0.025
        ) {
          point.glow =
            visual.environmentGlow *
            (
              0.45 +
              particle.chaosNoise *
                0.55
            ) *
            (
              0.55 +
              Math.max(
                escapeGate,
                wildGate,
              ) *
                0.45
            );
        } else {
          point.glow = 0;
        }
      }
    };

    const drawRoamingCache = (
      ctx:
        CanvasRenderingContext2D,
      cache:
        CachedRoamingPoint[],
      front:
        boolean,
    ) => {
      for (
        let index = 0;
        index < cache.length;
        index += 1
      ) {
        const point =
          cache[index];

        if (
          point.alpha <
          0.003
        ) {
          continue;
        }

        ctx.fillStyle =
          front
            ? `rgba(78,214,255,${point.alpha})`
            : `rgba(48,160,225,${point.alpha})`;

        ctx.beginPath();

        ctx.arc(
          point.x,
          point.y,
          point.size,
          0,
          Math.PI * 2,
        );

        ctx.fill();

        if (
          point.glow >
          0.06
        ) {
          drawGlowSprite(
            ctx,
            cyanGlowSprite,
            point.x,
            point.y,
            point.size,
            point.glow,
          );
        }
      }
    };

    const drawLegacyShell3Escape = (
      ctx:
        CanvasRenderingContext2D,
      centerX:
        number,
      centerY:
        number,
      baseRadius:
        number,
      tiltX:
        number,
      tiltZ:
        number,
      scale:
        number,
      luminosity:
        number,
      appReach:
        number,
    ) => {
      const geometryScale =
        renderSize / 460;

      for (
        let index = 0;
        index < shell3Particles.length;
        index += 1
      ) {
        const particle =
          shell3Particles[index];

        const t =
          index /
          shell3Particles.length;

        const lat =
          particle.latitude;

        const shellPhase =
          bodyRotationPhase +
          shell3DriftPhase;

        const roamWave =
          (
            Math.sin(
              t *
                Math.PI *
                4.4 -
                escapeSecondaryPhase *
                  2.1,
            ) +
            1
          ) /
          2;

        const roamGate =
          Math.pow(
            roamWave,
            2.2,
          );

        const escapeDistance =
          roamGate *
          visual.escapeStrength *
          appReach *
          0.88;

        const lateralWave =
          Math.sin(
            t *
              Math.PI *
              7.2 +
              escapeSecondaryPhase *
                1.4,
          ) *
          visual.escapeStrength *
          0.28;

        const twist =
          Math.sin(
            lat * 8 +
              shellPhase * 1.9 +
              escapeSecondaryPhase,
          ) *
            0.14 +
          lateralWave;

        const localWave =
          Math.sin(
            lat * 12 +
              shellPhase * 2.1,
          ) *
          visual.shell3Strength *
          6 *
          geometryScale;

        const radius =
          (
            baseRadius *
              0.992 +
            particle.radialNoise *
              geometryScale +
            localWave +
            escapeDistance
          ) *
          scale;

        const lowerMask =
          clamp01(
            (
              -Math.sin(lat) +
              0.25
            ) /
              1.25,
          );

        const sideField =
          (
            Math.sin(
              particle.longitude +
                shellPhase +
                violetPhase,
            ) +
            1
          ) /
          2;

        const sideMask =
          Math.pow(
            sideField,
            2.2,
          );

        const violetWave =
          (
            Math.sin(
              t *
                Math.PI *
                5.8 -
                violetPhase *
                  1.8 +
                Math.sin(
                  t *
                    Math.PI *
                    2.4 +
                    violetSecondaryPhase,
                ) *
                  visual.violetTwist,
            ) +
            1
          ) /
          2;

        const violetMask =
          Math.pow(
            violetWave,
            4.2,
          ) *
          (
            0.35 +
            lowerMask * 0.65
          ) *
          (
            0.45 +
            sideMask * 0.55
          ) *
          visual.violetStrength;

        const violetLift =
          violetMask *
          visual.violetTwist *
          geometryScale *
          11;

        const point =
          projectParticle(
            particle,
            radius +
              violetLift,
            bodyRotationPhase +
              shell3DriftPhase +
              transitionSpinPhase +
              twist,
            tiltX * 1.12,
            tiltZ * 0.78,
            centerX,
            centerY,
            renderSize,
          );

        const depth =
          clamp01(
            (
              point.z +
              baseRadius
            ) /
              (
                baseRadius * 2
              ),
          );

        const roamingLight =
          0.72 +
          roamGate * 0.28;

        const alpha =
          Math.min(
            0.88,
            visual.shell3Strength *
              luminosity *
              (
                0.24 +
                depth * 0.58
              ) *
              particle.brightnessNoise *
              roamingLight,
          );

        const particleSize =
          Math.max(
            0.07,
            particle.sizeNoise *
              geometryScale *
              (
                0.78 +
                depth * 0.7
              ) *
              (
                1 +
                visual.shell3Width *
                  0.55
              ),
          );

        ctx.fillStyle =
          `rgba(68,184,255,${alpha})`;

        ctx.beginPath();

        ctx.arc(
          point.x,
          point.y,
          particleSize,
          0,
          Math.PI * 2,
        );

        ctx.fill();

        if (
          particle.brightnessNoise >
            1.18 &&
          roamGate > 0.68
        ) {
          drawGlowSprite(
            ctx,
            cyanGlowSprite,
            point.x,
            point.y,
            particleSize,
            visual.particleGlow *
              roamGate *
              0.42,
          );
        }

        if (
          violetMask > 0.06
        ) {
          const violetAlpha =
            Math.min(
              0.68,
              violetMask *
                luminosity *
                (
                  0.25 +
                  depth * 0.6
                ) *
                particle.brightnessNoise,
            );

          const violetSize =
            particleSize *
            (
              0.9 +
              violetMask *
                visual.violetWidth *
                1.8
            );

          ctx.fillStyle =
            `rgba(135,96,255,${violetAlpha})`;

          ctx.beginPath();

          ctx.arc(
            point.x,
            point.y,
            violetSize,
            0,
            Math.PI * 2,
          );

          ctx.fill();

          if (
            violetMask > 0.76
          ) {
            drawGlowSprite(
              ctx,
              violetGlowSprite,
              point.x,
              point.y,
              violetSize,
              violetMask *
                visual.particleGlow *
                0.4,
            );
          }
        }
      }
    };

    const drawLegacyShell2Escape = (
      ctx:
        CanvasRenderingContext2D,
      centerX:
        number,
      centerY:
        number,
      baseRadius:
        number,
      tiltX:
        number,
      tiltZ:
        number,
      scale:
        number,
      luminosity:
        number,
      appReach:
        number,
    ) => {
      const geometryScale =
        renderSize / 460;

      for (
        let index = 0;
        index < shell2Particles.length;
        index += 1
      ) {
        const particle =
          shell2Particles[index];

        const lat =
          particle.latitude;

        const lon =
          particle.longitude;

        const shellPhase =
          bodyRotationPhase +
          shell2DriftPhase;

        const ribbonField =
          Math.sin(
            lon * 2.6 +
              lat * 4.8 -
              shellPhase * 1.4,
          );

        const ribbonMask =
          Math.pow(
            Math.max(
              0,
              (
                ribbonField +
                1
              ) /
                2,
            ),
            5.5,
          );

        if (
          ribbonMask <
          1 -
            visual.shell2Width
        ) {
          continue;
        }

        const escapeCarrier =
          (
            Math.sin(
              lon * 1.3 +
                lat * 1.9 -
                escapePhase * 2,
            ) +
            1
          ) /
          2;

        const escapeGate =
          Math.pow(
            escapeCarrier,
            2.35,
          );

        const escapeDistance =
          ribbonMask *
          escapeGate *
          visual.escapeStrength *
          appReach;

        const lateralWave =
          Math.sin(
            lon * 2 +
              lat * 3.2 +
              escapeSecondaryPhase *
                1.7,
          ) *
          escapeGate *
          visual.escapeStrength *
          0.34;

        const localWave =
          Math.sin(
            lat * 9 +
              shellPhase * 1.8,
          ) *
          4.4 *
          geometryScale;

        const radius =
          (
            baseRadius *
              1.018 +
            particle.radialNoise *
              geometryScale +
            ribbonMask *
              visual.shell2Strength *
              (
                8 *
                  geometryScale +
                localWave
              ) +
            escapeDistance
          ) *
          scale;

        const point =
          projectParticle(
            particle,
            radius,
            bodyRotationPhase +
              shell2DriftPhase +
              transitionSpinPhase +
              lateralWave,
            tiltX * 0.82,
            tiltZ * 1.15,
            centerX,
            centerY,
            renderSize,
          );

        const depth =
          clamp01(
            (
              point.z +
              baseRadius
            ) /
              (
                baseRadius * 2
              ),
          );

        const escapeLight =
          1 +
          escapeGate * 0.2;

        const alpha =
          Math.min(
            0.92,
            ribbonMask *
              visual.shell2Strength *
              luminosity *
              (
                0.32 +
                depth * 0.52
              ) *
              particle.brightnessNoise *
              escapeLight,
          );

        const particleSize =
          Math.max(
            0.07,
            particle.sizeNoise *
              geometryScale *
              (
                0.72 +
                depth * 0.78
              ) *
              (
                1 +
                ribbonMask * 0.95 +
                escapeGate * 0.15
              ),
          );

        ctx.fillStyle =
          `rgba(74,216,255,${alpha})`;

        ctx.beginPath();

        ctx.arc(
          point.x,
          point.y,
          particleSize,
          0,
          Math.PI * 2,
        );

        ctx.fill();

        if (
          particle.brightnessNoise >
            1.18 &&
          ribbonMask > 0.8 &&
          escapeGate > 0.62
        ) {
          drawGlowSprite(
            ctx,
            cyanGlowSprite,
            point.x,
            point.y,
            particleSize,
            visual.particleGlow *
              escapeGate *
              0.38,
          );
        }
      }
    };

    const spawnBurst = (
      time: number,
    ) => {
      const energy =
        visual.burstEnergy;

      const count =
        Math.round(
          7 +
            energy * 42 +
            Math.random() * 22,
        );

      const angle =
        Math.random() *
        Math.PI *
        2;

      for (
        let index = 0;
        index < count;
        index += 1
      ) {
        burstSparks.push({
          startTime:
            time +
            Math.random() * 0.18,

          duration:
            0.85 +
            Math.random() *
              (
                0.7 +
                energy
              ),

          angle:
            angle +
            (
              Math.random() -
              0.5
            ) *
              (
                0.5 +
                energy * 1.2
              ),

          distance:
            renderSize *
            (
              0.05 +
              energy * 0.12 +
              Math.random() *
                (
                  0.09 +
                  energy * 0.16
                )
            ),

          size:
            0.16 +
            Math.random() *
              (
                0.32 +
                energy * 0.55
              ),

          brightness:
            0.18 +
            energy * 0.3 +
            Math.random() * 0.42,

          tangent:
            (
              Math.random() -
              0.5
            ) *
            renderSize *
            0.1,

          far:
            presence.thinking >
            0.35,

          micro: false,
        });
      }
    };

    /*
     * =====================================
     * NEW
     * MICRO ENERGY ERUPTION
     *
     * Tiny burst emitted FROM a bright ridge.
     * Reuses BurstSpark system.
     * =====================================
     */
    const spawnMicroEruption = (
      time: number,
      eruptionAngle: number,
      ridgeStrength: number,
    ) => {
      const energy =
        visual.microEruptionEnergy *
        ridgeStrength;

      const count =
        Math.max(
          3,
          Math.round(
            3 +
              energy * 7 +
              Math.random() * 3,
          ),
        );

      for (
        let index = 0;
        index < count;
        index += 1
      ) {
        burstSparks.push({
          startTime:
            time +
            Math.random() * 0.055,

          duration:
            0.42 +
            Math.random() *
              (
                0.3 +
                energy * 0.22
              ),

          angle:
            eruptionAngle +
            (
              Math.random() -
              0.5
            ) *
              (
                0.13 +
                energy * 0.2
              ),

          distance:
            renderSize *
            (
              0.022 +
              energy * 0.045 +
              Math.random() *
                0.042
            ),

          size:
            0.11 +
            Math.random() *
              (
                0.18 +
                energy * 0.2
              ),

          brightness:
            0.42 +
            energy * 0.27 +
            Math.random() *
              0.23,

          tangent:
            (
              Math.random() -
              0.5
            ) *
            renderSize *
            0.025,

          far: false,

          micro: true,
        });
      }
    };

    const render = (
      timestamp: number,
    ) => {
      const time =
        timestamp / 1000;

      const deltaSeconds =
        previousTimestamp === 0
          ? 1 / 60
          : Math.max(
              0,
              Math.min(
                0.033,
                (
                  timestamp -
                  previousTimestamp
                ) /
                  1000,
              ),
            );

      previousTimestamp =
        timestamp;

      const currentState =
        stateRef.current;

      if (
        currentState !==
        lastState
      ) {
        lastState =
          currentState;

        transitionAge = 0;
      }

      transitionAge +=
        deltaSeconds;

      const target =
        VISUAL_STATES[
          currentState
        ];

      const amount =
        1 -
        Math.exp(
          -deltaSeconds * 2.05,
        );

      const keys =
        Object.keys(
          visual,
        ) as Array<
          keyof VisualState
        >;

      for (const key of keys) {
        visual[key] =
          lerp(
            visual[key],
            target[key],
            amount,
          );
      }

      presence.idle =
        lerp(
          presence.idle,
          currentState ===
            "idle"
            ? 1
            : 0,
          amount,
        );

      presence.listening =
        lerp(
          presence.listening,
          currentState ===
            "listening"
            ? 1
            : 0,
          amount,
        );

      presence.thinking =
        lerp(
          presence.thinking,
          currentState ===
            "thinking"
            ? 1
            : 0,
          amount,
        );

      presence.responding =
        lerp(
          presence.responding,
          currentState ===
            "responding"
            ? 1
            : 0,
          amount,
        );

      const transitionProgress =
        clamp01(
          transitionAge /
            transitionDuration,
        );

      const transitionEnvelope =
        Math.pow(
          Math.sin(
            transitionProgress *
              Math.PI,
          ),
          2,
        );

      transitionSpinPhase +=
        transitionEnvelope *
        1.9 *
        deltaSeconds;

      bodyRotationPhase +=
        visual.rotationSpeed *
        deltaSeconds;

      shell2DriftPhase +=
        visual.shell2Drift *
        deltaSeconds;

      shell3DriftPhase -=
        visual.shell3Drift *
        deltaSeconds;

      surfacePhase +=
        visual.surfaceSpeed *
        deltaSeconds;

      foldPhase +=
        visual.foldSpeed *
        deltaSeconds;

      foldSecondaryPhase +=
        (
          visual.foldSpeed *
            0.67 +
          0.05
        ) *
        deltaSeconds;

      escapePhase +=
        visual.escapeSpeed *
        deltaSeconds;

      escapeSecondaryPhase +=
        (
          visual.escapeSpeed *
            0.73 +
          0.035
        ) *
        deltaSeconds;

      chaosPhase +=
        visual.chaosSpeed *
        deltaSeconds;

      chaosSecondaryPhase +=
        (
          visual.chaosSpeed *
            0.71 +
          0.03
        ) *
        deltaSeconds;

      violetPhase +=
        (
          0.16 +
          visual.violetTwist *
            0.18
        ) *
        deltaSeconds;

      violetSecondaryPhase +=
        (
          0.11 +
          visual.violetTwist *
            0.14
        ) *
        deltaSeconds;

      breathPhase +=
        visual.breathSpeed *
        deltaSeconds;

      heartbeatPhase +=
        visual.heartbeatSpeed *
        deltaSeconds;

      const breathing =
        (
          Math.sin(
            breathPhase,
          ) +
          1
        ) /
        2;

      const beat1 =
        Math.pow(
          Math.max(
            0,
            Math.sin(
              heartbeatPhase,
            ),
          ),
          10,
        );

      const beat2 =
        Math.pow(
          Math.max(
            0,
            Math.sin(
              heartbeatPhase -
                0.92,
            ),
          ),
          14,
        ) *
        0.58;

      const heartbeat =
        clamp01(
          beat1 + beat2,
        );

      const scale =
        1 +
        (
          breathing -
          0.5
        ) *
          visual.breathScale *
          2 +
        heartbeat *
          visual.heartbeatScale;

      const luminosity =
        Math.min(
          1,
          visual.baseLuminosity +
            (
              breathing -
              0.5
            ) *
              visual.breathLight +
            heartbeat *
              visual.heartbeatLight,
        );

      const coreRect =
        coreCanvas.getBoundingClientRect();

      const appCenterX =
        coreRect.left +
        coreRect.width / 2;

      const appCenterY =
        coreRect.top +
        coreRect.height / 2;

      const localCenterX =
        renderSize / 2;

      const localCenterY =
        renderSize / 2;

      const baseRadius =
        renderSize * 0.342;

      const geometryScale =
        renderSize / 460;

      const appReach =
        Math.min(
          390,
          Math.max(
            renderSize * 0.55,
            Math.min(
              viewportWidth,
              viewportHeight,
            ) *
              0.36,
          ),
        );

      coreCtx.clearRect(
        0,
        0,
        renderSize,
        renderSize,
      );

      backCtx.clearRect(
        0,
        0,
        viewportWidth,
        viewportHeight,
      );

      frontCtx.clearRect(
        0,
        0,
        viewportWidth,
        viewportHeight,
      );

      const tiltX =
        Math.sin(
          time * 0.08,
        ) *
        0.088;

      const tiltZ =
        Math.sin(
          time * 0.055,
        ) *
          0.05 +
        Math.sin(
          time * 0.025,
        ) *
          0.02;

      updateRoamingCache(
        backRoamingParticles,
        cachedBackPoints,
        false,
        appCenterX,
        appCenterY,
        baseRadius,
      );

      updateRoamingCache(
        frontRoamingParticles,
        cachedFrontPoints,
        true,
        appCenterX,
        appCenterY,
        baseRadius,
      );

      drawLegacyShell3Escape(
        backCtx,
        appCenterX,
        appCenterY,
        baseRadius,
        tiltX,
        tiltZ,
        scale,
        luminosity,
        appReach,
      );

      drawRoamingCache(
        backCtx,
        cachedBackPoints,
        false,
      );

      /*
       * Best ridge candidate from THIS frame.
       * No new array and no sorting.
       */
      let eruptionCandidateStrength =
        0;

      let eruptionCandidateAngle =
        0;

      for (
        let index = 0;
        index < baseParticles.length;
        index += 1
      ) {
        const particle =
          baseParticles[index];

        const lat =
          particle.latitude;

        const lon =
          particle.longitude;

        const wave1 =
          Math.sin(
            lon * 3.7 +
              surfacePhase *
                particle.speedNoise +
              lat * 4.1,
          ) *
          (
            renderSize / 112
          );

        const wave2 =
          Math.cos(
            lat * 7.3 -
              surfacePhase * 0.62,
          ) *
          (
            renderSize / 210
          );

        const wave3 =
          Math.sin(
            lon * 8.1 -
              lat * 3.4 +
              surfacePhase * 0.74,
          ) *
          (
            renderSize / 285
          );

        const asymmetry =
          visual.asymmetry *
          (
            Math.sin(
              lon * 2.4 +
                surfacePhase *
                  0.28,
            ) *
              (
                renderSize / 180
              ) +
            Math.cos(
              lat * 3.3 -
                surfacePhase *
                  0.22,
            ) *
              (
                renderSize / 270
              )
          );

        const foldFieldA =
          Math.sin(
            lon * 2.25 +
              lat * 5.15 -
              foldPhase,
          );

        const foldMaskA =
          Math.pow(
            Math.max(
              0,
              (
                foldFieldA +
                1
              ) /
                2,
            ),
            7,
          );

        const foldFieldB =
          Math.sin(
            lon * 4.3 -
              lat * 3.8 +
              foldSecondaryPhase +
              1.25,
          );

        const foldMaskB =
          Math.pow(
            Math.max(
              0,
              (
                foldFieldB +
                1
              ) /
                2,
            ),
            8.5,
          );

        const crestField =
          Math.sin(
            lon * 2.25 +
              lat * 5.15 -
              foldPhase -
              0.43,
          );

        const crestMask =
          Math.pow(
            Math.max(
              0,
              (
                crestField +
                1
              ) /
                2,
            ),
            7.5,
          );

        const intersection =
          foldMaskA *
          foldMaskB;

        const ridgeField =
          (
            Math.sin(
              lon * 5.6 -
                lat * 4.2 +
                foldPhase * 0.86,
            ) +
            1
          ) /
          2;

        const ridgeMask =
          Math.pow(
            ridgeField,
            10,
          ) *
          Math.pow(
            crestMask,
            0.72,
          );

        const inward =
          (
            foldMaskA * 13.5 +
            foldMaskB * 9.5
          ) *
          visual.foldCompression *
          geometryScale;

        const outward =
          crestMask *
          visual.foldStrength *
          (
            15 +
            Math.sin(
              lat * 11.4 +
                foldSecondaryPhase,
            ) *
              5.8
          ) *
          geometryScale;

        const pinch =
          intersection *
          visual.foldStrength *
          (
            8.5 +
            Math.sin(
              lat * 16.5 +
                foldPhase,
            ) *
              3.6
          ) *
          geometryScale;

        const ridgeLift =
          ridgeMask *
          visual.ridgeGlow *
          geometryScale *
          5.5;

        const radius =
          (
            baseRadius +
            particle.radialNoise *
              geometryScale +
            (
              wave1 +
              wave2 +
              wave3 +
              asymmetry
            ) *
              visual.waveStrength -
            inward +
            outward +
            pinch +
            ridgeLift
          ) *
          scale;

        const rotation =
          bodyRotationPhase *
          (
            0.84 +
            Math.cos(lat) *
              0.24
          );

        const point =
          projectParticle(
            particle,
            radius,
            rotation +
              transitionSpinPhase,
            tiltX,
            tiltZ,
            localCenterX,
            localCenterY,
            renderSize,
          );

        const depth =
          (
            point.z +
            baseRadius
          ) /
          (
            baseRadius * 2
          );

        const dx =
          point.x -
          localCenterX;

        const dy =
          point.y -
          localCenterY;

        const normalizedRadius =
          Math.sqrt(
            dx * dx +
              dy * dy,
          ) /
          baseRadius;

        const innerStart = 0.45;
        const innerEnd = 0.99;

        let visibility = 1;

        if (
          normalizedRadius <=
          innerStart
        ) {
          visibility =
            0.012;
        } else if (
          normalizedRadius <
          innerEnd
        ) {
          let t =
            (
              normalizedRadius -
              innerStart
            ) /
            (
              innerEnd -
              innerStart
            );

          t =
            t *
            t *
            (
              3 -
              2 * t
            );

          visibility =
            0.012 +
            Math.pow(
              t,
              1.48,
            ) *
              0.988;
        }

        const foldDensity =
          (
            foldMaskA * 0.13 +
            foldMaskB * 0.1 +
            crestMask * 0.18 +
            intersection * 0.21 +
            ridgeMask * 0.12
          ) *
          visual.foldStrength;

        if (
          particle.centerNoise >
          clamp01(
            visibility +
              foldDensity,
          )
        ) {
          continue;
        }

        const stateBrightness =
          1 +
          presence.listening *
            0.12 +
          presence.thinking *
            0.2 +
          presence.responding *
            0.11;

        const foldLight =
          (
            crestMask * 0.48 +
            intersection * 0.32 -
            foldMaskA * 0.045
          ) *
          visual.foldBrightness;

        const ridgeLight =
          ridgeMask *
          visual.ridgeGlow *
          0.52;

        const alpha =
          Math.min(
            1,
            (
              0.095 +
              depth * 0.96
            ) *
              luminosity *
              Math.max(
                0.026,
                Math.pow(
                  visibility,
                  0.64,
                ),
              ) *
              particle.brightnessNoise *
              stateBrightness *
              (
                1.32 +
                foldLight +
                ridgeLight
              ),
          );

        const particleSize =
          Math.max(
            0.08,
            particle.sizeNoise *
              geometryScale *
              (
                0.64 +
                depth * 0.76
              ) *
              (
                1 +
                crestMask *
                  visual.foldStrength *
                  0.25 +
                ridgeMask *
                  visual.ridgeGlow *
                  0.18
              ),
          );

        const crestColor =
          clamp01(
            crestMask *
              visual.foldBrightness,
          );

        const ridgeColor =
          clamp01(
            ridgeMask *
              visual.ridgeGlow,
          );

        const red =
          Math.round(
            42 -
              crestColor * 12 +
              ridgeColor * 30 +
              depth * 18,
          );

        const green =
          Math.round(
            188 +
              crestColor * 48 +
              ridgeColor * 45 +
              depth * 48,
          );

        const blue =
          Math.round(
            232 +
              crestColor * 20 +
              ridgeColor * 20 +
              depth * 22,
          );

        coreCtx.fillStyle =
          `rgba(${red},${green},${blue},${alpha})`;

        coreCtx.beginPath();

        coreCtx.arc(
          point.x,
          point.y,
          particleSize,
          0,
          Math.PI * 2,
        );

        coreCtx.fill();

        if (
          particle.brightnessNoise >
            1.2 &&
          depth > 0.62
        ) {
          drawGlowSprite(
            coreCtx,
            cyanGlowSprite,
            point.x,
            point.y,
            particleSize,
            visual.particleGlow *
              0.27,
          );
        }

        if (
          ridgeMask > 0.7
        ) {
          drawGlowSprite(
            coreCtx,
            cyanGlowSprite,
            point.x,
            point.y,
            particleSize,
            ridgeMask *
              visual.ridgeGlow *
              0.45,
          );
        }

        /*
         * PERFORMANCE-SAFE RIDGE PICKER
         *
         * Check only 1 out of every
         * 13 visible particles.
         */
        if (
          index % 13 === 0 &&
          ridgeMask >
            eruptionCandidateStrength &&
          ridgeMask > 0.64 &&
          normalizedRadius >
            0.78 &&
          depth > 0.4
        ) {
          eruptionCandidateStrength =
            ridgeMask;

          eruptionCandidateAngle =
            Math.atan2(
              dy,
              dx,
            );
        }
      }

      /*
       * Trigger one tiny eruption only
       * when enough energy has accumulated.
       */
      microEruptionCharge +=
        visual.microEruptionRate *
        deltaSeconds;

      if (
        microEruptionCharge >=
          microEruptionThreshold &&
        eruptionCandidateStrength >
          0.64
      ) {
        spawnMicroEruption(
          time,
          eruptionCandidateAngle,
          eruptionCandidateStrength,
        );

        microEruptionCharge = 0;

        microEruptionThreshold =
          0.72 +
          Math.random() * 0.58;
      }

      /*
       * LOCAL A
       */
      for (
        let index = 0;
        index < shell2Particles.length;
        index += 1
      ) {
        const particle =
          shell2Particles[index];

        const lat =
          particle.latitude;

        const lon =
          particle.longitude;

        const shellPhase =
          bodyRotationPhase +
          shell2DriftPhase;

        const ribbonField =
          Math.sin(
            lon * 2.6 +
              lat * 4.8 -
              shellPhase * 1.4,
          );

        const ribbonMask =
          Math.pow(
            Math.max(
              0,
              (
                ribbonField +
                1
              ) /
                2,
            ),
            5.5,
          );

        if (
          ribbonMask < 0.35
        ) {
          continue;
        }

        const localWave =
          Math.sin(
            lat * 9 +
              shellPhase * 1.8,
          ) *
          4.4 *
          geometryScale;

        const radius =
          (
            baseRadius *
              1.018 +
            particle.radialNoise *
              geometryScale +
            ribbonMask *
              visual.shell2Strength *
              (
                8 *
                  geometryScale +
                localWave
              )
          ) *
          scale;

        const point =
          projectParticle(
            particle,
            radius,
            bodyRotationPhase +
              shell2DriftPhase +
              transitionSpinPhase,
            tiltX * 0.82,
            tiltZ * 1.15,
            localCenterX,
            localCenterY,
            renderSize,
          );

        const depth =
          clamp01(
            (
              point.z +
              baseRadius
            ) /
              (
                baseRadius * 2
              ),
          );

        const alpha =
          Math.min(
            0.9,
            ribbonMask *
              visual.shell2Strength *
              luminosity *
              (
                0.25 +
                depth * 0.58
              ) *
              particle.brightnessNoise,
          );

        const particleSize =
          Math.max(
            0.08,
            particle.sizeNoise *
              geometryScale *
              (
                0.9 +
                ribbonMask * 0.72
              ),
          );

        coreCtx.fillStyle =
          `rgba(74,216,255,${alpha})`;

        coreCtx.beginPath();

        coreCtx.arc(
          point.x,
          point.y,
          particleSize,
          0,
          Math.PI * 2,
        );

        coreCtx.fill();
      }

      /*
       * LOCAL B + VIOLET
       */
      for (
        let index = 0;
        index < shell3Particles.length;
        index += 1
      ) {
        const particle =
          shell3Particles[index];

        const lat =
          particle.latitude;

        const t =
          index /
          shell3Particles.length;

        const twist =
          Math.sin(
            lat * 8 +
              bodyRotationPhase *
                1.9,
          ) *
          0.14;

        const localVioletWave =
          (
            Math.sin(
              t *
                Math.PI *
                5.8 -
                violetPhase *
                  1.8 +
                Math.sin(
                  t *
                    Math.PI *
                    2.4 +
                    violetSecondaryPhase,
                ) *
                  visual.violetTwist,
            ) +
            1
          ) /
          2;

        const lowerMask =
          clamp01(
            (
              -Math.sin(lat) +
              0.25
            ) /
              1.25,
          );

        const violetMask =
          Math.pow(
            localVioletWave,
            4.2,
          ) *
          (
            0.3 +
            lowerMask * 0.7
          ) *
          visual.violetStrength;

        const radius =
          (
            baseRadius *
              0.992 +
            particle.radialNoise *
              geometryScale +
            Math.sin(
              lat * 12 +
                surfacePhase * 1.2,
            ) *
              visual.shell3Strength *
              6 *
              geometryScale +
            violetMask *
              visual.violetTwist *
              geometryScale *
              8
          ) *
          scale;

        const point =
          projectParticle(
            particle,
            radius,
            bodyRotationPhase +
              shell3DriftPhase +
              transitionSpinPhase +
              twist +
              violetMask *
                visual.violetTwist *
                0.06,
            tiltX * 1.12,
            tiltZ * 0.78,
            localCenterX,
            localCenterY,
            renderSize,
          );

        const depth =
          clamp01(
            (
              point.z +
              baseRadius
            ) /
              (
                baseRadius * 2
              ),
          );

        const alpha =
          Math.min(
            0.88,
            visual.shell3Strength *
              luminosity *
              (
                0.24 +
                depth * 0.62
              ) *
              particle.brightnessNoise,
          );

        const particleSize =
          Math.max(
            0.08,
            particle.sizeNoise *
              geometryScale *
              1.08,
          );

        coreCtx.fillStyle =
          `rgba(70,186,255,${alpha})`;

        coreCtx.beginPath();

        coreCtx.arc(
          point.x,
          point.y,
          particleSize,
          0,
          Math.PI * 2,
        );

        coreCtx.fill();

        if (
          violetMask > 0.06
        ) {
          const violetAlpha =
            Math.min(
              0.66,
              violetMask *
                luminosity *
                (
                  0.25 +
                  depth * 0.58
                ),
            );

          const violetSize =
            particleSize *
            (
              0.92 +
              violetMask *
                visual.violetWidth *
                1.7
            );

          coreCtx.fillStyle =
            `rgba(135,96,255,${violetAlpha})`;

          coreCtx.beginPath();

          coreCtx.arc(
            point.x,
            point.y,
            violetSize,
            0,
            Math.PI * 2,
          );

          coreCtx.fill();

          if (
            violetMask > 0.78
          ) {
            drawGlowSprite(
              coreCtx,
              violetGlowSprite,
              point.x,
              point.y,
              violetSize,
              violetMask *
                visual.particleGlow *
                0.36,
            );
          }
        }
      }

      /*
       * LOCAL DUST
       */
      const localActivity =
        0.72 +
        presence.listening * 0.2 +
        presence.thinking * 0.5 +
        presence.responding * 0.3;

      for (
        let index = 0;
        index < ambientParticles.length;
        index += 1
      ) {
        const ambient =
          ambientParticles[index];

        const angle =
          ambient.angle +
          time *
            ambient.speed *
            localActivity;

        const radius =
          baseRadius *
            ambient.radiusFactor +
          Math.sin(
            time * 0.4 +
              ambient.phase,
          ) *
            renderSize *
            0.012;

        const x =
          localCenterX +
          Math.cos(angle) *
            radius;

        const y =
          localCenterY +
          Math.sin(angle) *
            radius *
            0.91;

        coreCtx.fillStyle =
          `rgba(80,205,255,${
            ambient.alpha *
            luminosity *
            localActivity
          })`;

        coreCtx.beginPath();

        coreCtx.arc(
          x,
          y,
          Math.max(
            0.06,
            ambient.size *
              geometryScale,
          ),
          0,
          Math.PI * 2,
        );

        coreCtx.fill();
      }

      /*
       * EXISTING LARGE BURST SYSTEM
       */
      burstCharge +=
        visual.burstRate *
        deltaSeconds;

      if (
        burstCharge >=
        burstThreshold
      ) {
        spawnBurst(time);

        burstCharge = 0;

        burstThreshold =
          0.74 +
          Math.random() * 0.36;
      }

      /*
       * SHARED SPARK RENDERER
       *
       * Handles both normal bursts
       * and the new Micro Eruptions.
       */
      for (
        let index =
          burstSparks.length - 1;
        index >= 0;
        index -= 1
      ) {
        const spark =
          burstSparks[index];

        const progress =
          (
            time -
            spark.startTime
          ) /
          spark.duration;

        if (
          progress < 0
        ) {
          continue;
        }

        if (
          progress >= 1
        ) {
          burstSparks.splice(
            index,
            1,
          );

          continue;
        }

        const outward =
          Math.pow(
            progress,
            spark.micro
              ? 0.66
              : spark.far
                ? 0.72
                : 0.82,
          ) *
          spark.distance;

        const tangent =
          Math.sin(
            progress *
              Math.PI,
          ) *
          spark.tangent;

        const radius =
          baseRadius +
          outward;

        const x =
          localCenterX +
          Math.cos(
            spark.angle,
          ) *
            radius -
          Math.sin(
            spark.angle,
          ) *
            tangent;

        const y =
          localCenterY +
          Math.sin(
            spark.angle,
          ) *
            radius *
            0.92 +
          Math.cos(
            spark.angle,
          ) *
            tangent *
            0.92;

        const alpha =
          Math.pow(
            1 - progress,
            spark.micro
              ? 1.15
              : spark.far
                ? 1.25
                : 1.62,
          ) *
          spark.brightness *
          luminosity;

        const sparkSize =
          Math.max(
            0.07,
            spark.size *
              geometryScale,
          );

        coreCtx.fillStyle =
          spark.micro
            ? `rgba(145,239,255,${alpha})`
            : `rgba(110,225,255,${alpha})`;

        coreCtx.beginPath();

        coreCtx.arc(
          x,
          y,
          sparkSize,
          0,
          Math.PI * 2,
        );

        coreCtx.fill();

        /*
         * Only strongest micro sparks
         * receive sprite glow.
         */
        if (
          spark.brightness >
            (
              spark.micro
                ? 0.72
                : 0.7
            ) &&
          alpha > 0.18
        ) {
          drawGlowSprite(
            coreCtx,
            cyanGlowSprite,
            x,
            y,
            sparkSize,
            visual.particleGlow *
              alpha *
              (
                spark.micro
                  ? 0.38
                  : 0.45
              ),
          );
        }
      }

      drawLegacyShell2Escape(
        frontCtx,
        appCenterX,
        appCenterY,
        baseRadius,
        tiltX,
        tiltZ,
        scale,
        luminosity,
        appReach,
      );

      drawRoamingCache(
        frontCtx,
        cachedFrontPoints,
        true,
      );

      animationFrame =
        requestAnimationFrame(
          render,
        );
    };

    animationFrame =
      requestAnimationFrame(
        render,
      );

    return () => {
      cancelAnimationFrame(
        animationFrame,
      );

      resizeObserver.disconnect();

      window.removeEventListener(
        "resize",
        onWindowResize,
      );
    };
  }, [size]);

  return (
    <>
      <canvas
        ref={coreCanvasRef}
        aria-label={`Qronos Orb ${state}`}
        style={{
          position: "absolute",
          left: "50%",
          top: "50%",
          transform:
            "translate(-50%, -50%)",
          zIndex: 4,
          pointerEvents: "none",
        }}
      />

      {typeof document !==
        "undefined" &&
        createPortal(
          <canvas
            ref={
              backEffectsCanvasRef
            }
            aria-hidden="true"
            style={{
              position: "fixed",
              left: 0,
              top: 0,
              width: "100vw",
              height: "100vh",
              zIndex: 3,
              pointerEvents:
                "none",
            }}
          />,
          document.body,
        )}

      {typeof document !==
        "undefined" &&
        createPortal(
          <canvas
            ref={
              frontEffectsCanvasRef
            }
            aria-hidden="true"
            style={{
              position: "fixed",
              left: 0,
              top: 0,
              width: "100vw",
              height: "100vh",
              zIndex: 5,
              pointerEvents:
                "none",
            }}
          />,
          document.body,
        )}
    </>
  );
}

export default QronosOrb;