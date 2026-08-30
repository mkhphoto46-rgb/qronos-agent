import {
  useEffect,
  useMemo,
  useRef,
} from "react";

import "./CrossViewParticleTransition.css";

type CrossViewTarget =
  | "conversations"
  | "library"
  | null;

type CrossViewParticleTransitionProps = {
  target: CrossViewTarget;
};

type LocalParticle = {
  id: number;
  group:
    | "source"
    | "target";
  angle: number;
  radius: number;
  drift: number;
  size: number;
  alpha: number;
  wobble: number;
  tone:
    | "cyan"
    | "indigo"
    | "white";
};

const MAX_DPR = 1;
const DURATION_MS = 360;

const SOURCE_COUNT = 250;
const TARGET_COUNT = 250;

function clamp01(
  value: number,
) {
  return Math.max(
    0,
    Math.min(
      1,
      value,
    ),
  );
}

function smoothStep(
  value: number,
) {
  const t =
    clamp01(
      value,
    );

  return (
    t *
    t *
    (
      3 -
      2 *
        t
    )
  );
}

function smootherStep(
  value: number,
) {
  const t =
    clamp01(
      value,
    );

  return (
    t *
    t *
    t *
    (
      t *
        (
          t * 6 -
          15
        ) +
      10
    )
  );
}

function seededRandom(
  seed: number,
) {
  let value =
    seed >>> 0;

  return () => {
    value =
      (
        value *
          1664525 +
        1013904223
      ) >>>
      0;

    return (
      value /
      4294967296
    );
  };
}

function makeParticles() {
  const random =
    seededRandom(
      441903,
    );

  const build =
    (
      group:
        | "source"
        | "target",
      count: number,
      offset: number,
    ) =>
      Array.from(
        {
          length:
            count,
        },
        (
          _,
          index,
        ) => {
          const toneRoll =
            random();

          return {
            id:
              offset +
              index,

            group,

            angle:
              random() *
              Math.PI *
              2,

            radius:
              24 +
              random() *
                96,

            drift:
              0.7 +
              random() *
                1.5,

            size:
              index % 10 ===
              0
                ? 0.7 +
                  random() *
                    0.7
                : 0.25 +
                  random() *
                    0.45,

            alpha:
              0.22 +
              random() *
                0.55,

            wobble:
              (
                random() -
                0.5
              ) *
              22,

            tone:
              toneRoll >
              0.92
                ? "white"
                : toneRoll >
                    0.72
                  ? "indigo"
                  : "cyan",
          } satisfies LocalParticle;
        },
      );

  return [
    ...build(
      "source",
      SOURCE_COUNT,
      0,
    ),
    ...build(
      "target",
      TARGET_COUNT,
      SOURCE_COUNT,
    ),
  ];
}

const localParticles =
  makeParticles();

function CrossViewParticleTransition({
  target,
}: CrossViewParticleTransitionProps) {
  const canvasRef =
    useRef<HTMLCanvasElement | null>(
      null,
    );

  const targetRef =
    useRef<CrossViewTarget>(
      target,
    );

  const startedAtRef =
    useRef(
      performance.now(),
    );

  useEffect(() => {
    targetRef.current =
      target;

    if (target) {
      startedAtRef.current =
        performance.now();
    }
  }, [target]);

  const activeClass =
    useMemo(
      () =>
        target
          ? `cross-view-particle-transition-active cross-view-particle-transition-to-${target}`
          : "",
      [target],
    );

  useEffect(() => {
    const canvas =
      canvasRef.current;

    if (!canvas) {
      return;
    }

    const context =
      canvas.getContext(
        "2d",
        {
          alpha: true,
        },
      );

    if (!context) {
      return;
    }

    let width = 1;
    let height = 1;
    let dpr = 1;
    let frame = 0;
    let visible =
      !document.hidden;

    const resize =
      () => {
        width =
          Math.max(
            1,
            window.innerWidth,
          );

        height =
          Math.max(
            1,
            window.innerHeight,
          );

        dpr =
          Math.min(
            window.devicePixelRatio ||
              1,
            MAX_DPR,
          );

        canvas.width =
          Math.round(
            width * dpr,
          );

        canvas.height =
          Math.round(
            height * dpr,
          );

        canvas.style.width =
          `${width}px`;

        canvas.style.height =
          `${height}px`;

        context.setTransform(
          dpr,
          0,
          0,
          dpr,
          0,
          0,
        );
      };

    resize();

    const handleResize =
      () => {
        resize();
      };

    const handleVisibility =
      () => {
        visible =
          !document.hidden;
      };

    window.addEventListener(
      "resize",
      handleResize,
    );

    document.addEventListener(
      "visibilitychange",
      handleVisibility,
    );

    const drawParticle =
      (
        x: number,
        y: number,
        particle: LocalParticle,
        alpha: number,
      ) => {
        if (
          alpha <=
          0.008
        ) {
          return;
        }

        const fill =
          particle.tone ===
          "indigo"
            ? "rgba(107,125,255,1)"
            : particle.tone ===
                "white"
              ? "rgba(245,254,255,1)"
              : "rgba(72,223,255,1)";

        context.globalAlpha =
          Math.min(
            1,
            alpha,
          );

        context.fillStyle =
          fill;

        context.beginPath();

        context.arc(
          x,
          y,
          particle.size,
          0,
          Math.PI *
            2,
        );

        context.fill();

        if (
          particle.id %
            31 ===
          0
        ) {
          context.globalAlpha =
            Math.min(
              1,
              alpha *
                0.13,
            );

          context.beginPath();

          context.arc(
            x,
            y,
            particle.size *
              4,
            0,
            Math.PI *
              2,
          );

          context.fill();
        }
      };

    const render =
      (
        timestamp: number,
      ) => {
        frame =
          window.requestAnimationFrame(
            render,
          );

        if (!visible) {
          return;
        }

        context.clearRect(
          0,
          0,
          width,
          height,
        );

        const currentTarget =
          targetRef.current;

        if (!currentTarget) {
          return;
        }

        const elapsed =
          timestamp -
          startedAtRef.current;

        const raw =
          clamp01(
            elapsed /
              DURATION_MS,
          );

        if (
          raw >=
          1
        ) {
          return;
        }

        const p =
          smootherStep(
            raw,
          );

        const time =
          timestamp *
          0.001;

        const toConversations =
          currentTarget ===
          "conversations";

        /*
         * New design:
         * no particles travel between sides.
         * Source particles dissolve locally.
         * Target particles form locally.
         * Middle of screen is handled by a soft CSS phase veil.
         */

        const sourceX =
          width *
          (
            toConversations
              ? 0.865
              : 0.135
          );

        const sourceY =
          height *
          (
            toConversations
              ? 0.54
              : 0.48
          );

        const targetX =
          width *
          (
            toConversations
              ? 0.135
              : 0.865
          );

        const targetY =
          height *
          (
            toConversations
              ? 0.48
              : 0.54
          );

        for (
          const particle of
          localParticles
        ) {
          if (
            particle.group ===
            "source"
          ) {
            const dissolve =
              smoothStep(
                p /
                  0.48,
              );

            const angle =
              particle.angle +
              time *
                particle.drift *
                0.8;

            const radius =
              particle.radius *
              (
                0.82 +
                dissolve *
                  0.9
              );

            const x =
              sourceX +
              Math.cos(
                angle,
              ) *
                radius +
              Math.sin(
                particle.angle *
                  1.8,
              ) *
                particle.wobble *
                dissolve;

            const y =
              sourceY +
              Math.sin(
                angle *
                1.07,
              ) *
                radius *
                0.62 +
              Math.cos(
                particle.angle *
                  1.3,
              ) *
                particle.wobble *
                0.55 *
                dissolve;

            const alpha =
              particle.alpha *
              (
                1 -
                dissolve
              ) *
              (
                0.72 +
                (
                  (
                    Math.sin(
                      angle *
                        0.7,
                    ) +
                    1
                  ) /
                  2
                ) *
                  0.28
              );

            drawParticle(
              x,
              y,
              particle,
              alpha,
            );

            continue;
          }

          const birth =
            smoothStep(
              (
                p -
                0.42
              ) /
                0.5,
            );

          if (
            birth <=
            0
          ) {
            continue;
          }

          const angle =
            particle.angle -
            time *
              particle.drift *
              0.75;

          const radius =
            particle.radius *
            (
              1.65 -
              birth *
                0.75
            );

          const x =
            targetX +
            Math.cos(
              angle,
            ) *
              radius +
            Math.sin(
              particle.angle *
                1.6,
            ) *
              particle.wobble *
              (
                1 -
                birth
              );

          const y =
            targetY +
            Math.sin(
              angle *
                1.05,
            ) *
              radius *
              0.62 +
            Math.cos(
              particle.angle *
                1.2,
            ) *
              particle.wobble *
              0.5 *
              (
                1 -
                birth
              );

          const alpha =
            particle.alpha *
            birth *
            (
              0.72 +
              (
                (
                  Math.cos(
                    angle *
                      0.68,
                  ) +
                  1
                ) /
                2
              ) *
                0.28
            );

          drawParticle(
            x,
            y,
            particle,
            alpha,
          );
        }

        context.globalAlpha =
          1;
      };

    frame =
      window.requestAnimationFrame(
        render,
      );

    return () => {
      window.cancelAnimationFrame(
        frame,
      );

      window.removeEventListener(
        "resize",
        handleResize,
      );

      document.removeEventListener(
        "visibilitychange",
        handleVisibility,
      );
    };
  }, []);

  return (
    <div
      className={`cross-view-particle-transition ${activeClass}`}
      aria-hidden="true"
    >
      <canvas
        ref={
          canvasRef
        }
      />

      <span className="cross-view-phase-veil" />
      <span className="cross-view-source-glow" />
      <span className="cross-view-target-glow" />
    </div>
  );
}

export default CrossViewParticleTransition;
