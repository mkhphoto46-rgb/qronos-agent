import { useEffect, useRef } from "react";
import type { OrbState } from "./OrbState";

export type OrbTaskResult =
  | "done"
  | "failed"
  | null;

type OrbTaskRendererProps = {
  state: OrbState;
  taskResult?: OrbTaskResult;
};

type Point = {
  x: number;
  y: number;
};

type MorphParticle = {
  sourceX: number;
  sourceY: number;

  targetX: number;
  targetY: number;

  phase: number;
  size: number;
  brightness: number;
};

type DigitMap = Record<
  string,
  number[][]
>;

type GlyphMap = Record<
  string,
  number[][]
>;

function OrbTaskRenderer({
  state,
  taskResult = null,
}: OrbTaskRendererProps) {
  const canvasRef =
    useRef<HTMLCanvasElement | null>(null);

  const stateRef =
    useRef<OrbState>(state);

  const resultRef =
    useRef<OrbTaskResult>(
      taskResult,
    );

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    resultRef.current =
      taskResult;
  }, [taskResult]);

  useEffect(() => {
    const canvas =
      canvasRef.current;

    if (!canvas) {
      return;
    }

    const ctx =
      canvas.getContext("2d", {
        alpha: true,
        desynchronized: true,
      });

    if (!ctx) {
      return;
    }

    /*
     * APPROVED SIZE LOCK
     */
    const logicalSize = 248;

    const dpr =
      Math.min(
        window.devicePixelRatio || 1,
        1.5,
      );

    canvas.width =
      Math.round(
        logicalSize * dpr,
      );

    canvas.height =
      Math.round(
        logicalSize * dpr,
      );

    canvas.style.width =
      `${logicalSize}px`;

    canvas.style.height =
      `${logicalSize}px`;

    ctx.setTransform(
      dpr,
      0,
      0,
      dpr,
      0,
      0,
    );

    const centerX =
      logicalSize / 2;

    const centerY = 108;

    /*
     * ==================================================
     * FINAL TASK SHAPE
     *
     * No line-following animation.
     * These are only destination points.
     * ==================================================
     */

    const targetPoints: Point[] =
      [];

    const addPoint = (
      x: number,
      y: number,
    ) => {
      targetPoints.push({
        x,
        y,
      });
    };

    const addLinePoints = (
      x1: number,
      y1: number,
      x2: number,
      y2: number,
      spacing = 2.3,
    ) => {
      const dx =
        x2 - x1;

      const dy =
        y2 - y1;

      const distance =
        Math.sqrt(
          dx * dx +
            dy * dy,
        );

      const steps =
        Math.max(
          1,
          Math.ceil(
            distance /
              spacing,
          ),
        );

      for (
        let index = 0;
        index <= steps;
        index += 1
      ) {
        const t =
          index / steps;

        addPoint(
          x1 +
            dx * t,

          y1 +
            dy * t,
        );
      }
    };

    const addDottedConnector = (
      x1: number,
      x2: number,
      y: number,
      count: number,
    ) => {
      for (
        let index = 0;
        index < count;
        index += 1
      ) {
        const t =
          count <= 1
            ? 0
            : index /
              (
                count - 1
              );

        addPoint(
          x1 +
            (
              x2 - x1
            ) *
              t,
          y,
        );
      }
    };

    /*
     * LEFT PARTICLE FLOW
     */
    addDottedConnector(
      10,
      36,
      centerY,
      7,
    );

    /*
     * FILE ICON
     */
    addLinePoints(
      42,
      84,
      60,
      84,
    );

    addLinePoints(
      60,
      84,
      70,
      94,
    );

    addLinePoints(
      70,
      94,
      70,
      132,
    );

    addLinePoints(
      70,
      132,
      42,
      132,
    );

    addLinePoints(
      42,
      132,
      42,
      84,
    );

    addLinePoints(
      60,
      84,
      60,
      94,
      2,
    );

    addLinePoints(
      60,
      94,
      70,
      94,
      2,
    );

    addDottedConnector(
      49,
      63,
      104,
      5,
    );

    addDottedConnector(
      49,
      61,
      112,
      4,
    );

    addDottedConnector(
      49,
      63,
      120,
      5,
    );

    /*
     * FILE → PRINTER
     */
    addDottedConnector(
      76,
      139,
      centerY,
      13,
    );

    /*
     * PRINTER
     */
    addLinePoints(
      147,
      98,
      188,
      98,
    );

    addLinePoints(
      188,
      98,
      188,
      132,
    );

    addLinePoints(
      188,
      132,
      147,
      132,
    );

    addLinePoints(
      147,
      132,
      147,
      98,
    );

    /*
     * paper
     */
    addLinePoints(
      155,
      98,
      155,
      82,
    );

    addLinePoints(
      155,
      82,
      180,
      82,
    );

    addLinePoints(
      180,
      82,
      180,
      98,
    );

    /*
     * output
     */
    addLinePoints(
      155,
      132,
      155,
      143,
    );

    addLinePoints(
      155,
      143,
      180,
      143,
    );

    addLinePoints(
      180,
      143,
      180,
      132,
    );

    /*
     * printer indicator
     */
    addPoint(
      180,
      108,
    );

    /*
     * RIGHT PARTICLE FLOW
     */
    addDottedConnector(
      194,
      238,
      centerY,
      10,
    );

    /*
     * ==================================================
     * MORPH PARTICLES
     *
     * Every destination receives a stable
     * outside-Orb source.
     * ==================================================
     */

    const particles: MorphParticle[] =
      [];

    for (
      let index = 0;
      index < targetPoints.length;
      index += 1
    ) {
      const target =
        targetPoints[index];

      const seedA =
        Math.sin(
          index * 17.713,
        ) *
        31731.17;

      const seedB =
        Math.sin(
          index * 41.317,
        ) *
        17113.71;

      const seedC =
        Math.sin(
          index * 71.131,
        ) *
        27317.31;

      const angle =
        (
          index /
          targetPoints.length
        ) *
          Math.PI *
          2 +
        Math.sin(seedA) *
          0.8;

      const radius =
        92 +
        (
          (
            Math.sin(seedB) +
            1
          ) /
          2
        ) *
          30;

      particles.push({
        sourceX:
          centerX +
          Math.cos(angle) *
            radius,

        sourceY:
          centerY +
          Math.sin(angle) *
            radius *
            0.74,

        targetX:
          target.x,

        targetY:
          target.y,

        phase:
          (
            (
              Math.sin(seedC) +
              1
            ) /
            2
          ) *
          Math.PI *
          2,

        size:
          0.75 +
          (
            (
              Math.cos(seedB) +
              1
            ) /
            2
          ) *
            0.65,

        brightness:
          0.65 +
          (
            (
              Math.sin(seedA) +
              1
            ) /
            2
          ) *
            0.35,
      });
    }

    /*
     * ==================================================
     * DRAW
     * ==================================================
     */

    const drawParticle = (
      x: number,
      y: number,
      radius: number,
      alpha: number,
      hot = false,
    ) => {
      if (
        alpha <= 0.002
      ) {
        return;
      }

      ctx.beginPath();

      ctx.fillStyle =
        hot
          ? `rgba(201,251,255,${alpha})`
          : `rgba(77,219,255,${alpha})`;

      ctx.arc(
        x,
        y,
        radius,
        0,
        Math.PI * 2,
      );

      ctx.fill();
    };

    const drawHotParticle = (
      x: number,
      y: number,
      radius: number,
      alpha: number,
    ) => {
      ctx.beginPath();

      ctx.fillStyle =
        `rgba(88,220,255,${
          alpha * 0.12
        })`;

      ctx.arc(
        x,
        y,
        radius * 3.2,
        0,
        Math.PI * 2,
      );

      ctx.fill();

      drawParticle(
        x,
        y,
        radius,
        alpha,
        true,
      );
    };

    const smoothstep = (
      value: number,
    ) => {
      const t =
        Math.max(
          0,
          Math.min(
            1,
            value,
          ),
        );

      return (
        t *
        t *
        (
          3 -
          2 * t
        )
      );
    };

    /*
     * ==================================================
     * NUMBER / TEXT MATRICES
     * ==================================================
     */

    const DIGITS: DigitMap = {
      "0": [
        [1, 1, 1],
        [1, 0, 1],
        [1, 0, 1],
        [1, 0, 1],
        [1, 1, 1],
      ],

      "1": [
        [0, 1, 0],
        [1, 1, 0],
        [0, 1, 0],
        [0, 1, 0],
        [1, 1, 1],
      ],

      "2": [
        [1, 1, 1],
        [0, 0, 1],
        [1, 1, 1],
        [1, 0, 0],
        [1, 1, 1],
      ],

      "3": [
        [1, 1, 1],
        [0, 0, 1],
        [1, 1, 1],
        [0, 0, 1],
        [1, 1, 1],
      ],

      "4": [
        [1, 0, 1],
        [1, 0, 1],
        [1, 1, 1],
        [0, 0, 1],
        [0, 0, 1],
      ],

      "5": [
        [1, 1, 1],
        [1, 0, 0],
        [1, 1, 1],
        [0, 0, 1],
        [1, 1, 1],
      ],

      "6": [
        [1, 1, 1],
        [1, 0, 0],
        [1, 1, 1],
        [1, 0, 1],
        [1, 1, 1],
      ],

      "7": [
        [1, 1, 1],
        [0, 0, 1],
        [0, 0, 1],
        [0, 0, 1],
        [0, 0, 1],
      ],

      "8": [
        [1, 1, 1],
        [1, 0, 1],
        [1, 1, 1],
        [1, 0, 1],
        [1, 1, 1],
      ],

      "9": [
        [1, 1, 1],
        [1, 0, 1],
        [1, 1, 1],
        [0, 0, 1],
        [1, 1, 1],
      ],
    };

    const GLYPHS: GlyphMap = {
      D: [
        [1, 1, 0],
        [1, 0, 1],
        [1, 0, 1],
        [1, 0, 1],
        [1, 1, 0],
      ],

      O: [
        [0, 1, 0],
        [1, 0, 1],
        [1, 0, 1],
        [1, 0, 1],
        [0, 1, 0],
      ],

      N: [
        [1, 0, 1],
        [1, 1, 1],
        [1, 1, 1],
        [1, 0, 1],
        [1, 0, 1],
      ],

      E: [
        [1, 1, 1],
        [1, 0, 0],
        [1, 1, 0],
        [1, 0, 0],
        [1, 1, 1],
      ],

      F: [
        [1, 1, 1],
        [1, 0, 0],
        [1, 1, 0],
        [1, 0, 0],
        [1, 0, 0],
      ],

      A: [
        [0, 1, 0],
        [1, 0, 1],
        [1, 1, 1],
        [1, 0, 1],
        [1, 0, 1],
      ],

      I: [
        [1, 1, 1],
        [0, 1, 0],
        [0, 1, 0],
        [0, 1, 0],
        [1, 1, 1],
      ],

      L: [
        [1, 0, 0],
        [1, 0, 0],
        [1, 0, 0],
        [1, 0, 0],
        [1, 1, 1],
      ],
    };

    /*
     * ==================================================
     * GENERATE PARTICLE TEXT TARGETS
     * ==================================================
     */

    const createNumberPoints = (
      value: number,
    ) => {
      const points: Point[] =
        [];

      const text =
        `${value}`;

      const scale = 2.5;

      const digitWidth =
        scale * 3 * 1.5;

      const spacing = 5;

      const percentWidth = 12;

      const totalWidth =
        text.length *
          digitWidth +
        (
          text.length -
          1
        ) *
          spacing +
        percentWidth;

      let x =
        centerX -
        totalWidth / 2;

      const y = 164;

      for (
        const digit of text
      ) {
        const matrix =
          DIGITS[digit];

        for (
          let row = 0;
          row < matrix.length;
          row += 1
        ) {
          for (
            let column = 0;
            column <
            matrix[row].length;
            column += 1
          ) {
            if (
              matrix[row][column] !==
              1
            ) {
              continue;
            }

            points.push({
              x:
                x +
                column *
                  scale *
                  1.5,

              y:
                y +
                row *
                  scale *
                  1.5,
            });
          }
        }

        x +=
          digitWidth +
          spacing;
      }

      /*
       * percent
       */
      points.push({
        x: x + 1,
        y: y + 1,
      });

      points.push({
        x: x + 9,
        y: y + 15,
      });

      for (
        let index = 0;
        index < 6;
        index += 1
      ) {
        points.push({
          x:
            x +
            2 +
            index * 1.18,

          y:
            y +
            13 -
            index * 2.05,
        });
      }

      return points;
    };

    const createResultPoints = (
      result: OrbTaskResult,
    ) => {
      const points: Point[] =
        [];

      if (!result) {
        return points;
      }

      const text =
        result === "failed"
          ? "FAILED"
          : "DONE";

      const scale = 2;

      const letterSpacing = 4;

      const widths =
        text.split("").map(
          (letter) =>
            GLYPHS[letter][0]
              .length *
            scale *
            1.5,
        );

      const totalWidth =
        widths.reduce(
          (
            total,
            width,
          ) =>
            total + width,
          0,
        ) +
        (
          text.length -
          1
        ) *
          letterSpacing;

      let x =
        centerX -
        totalWidth / 2;

      const y = 163;

      for (
        let letterIndex = 0;
        letterIndex <
        text.length;
        letterIndex += 1
      ) {
        const letter =
          text[letterIndex];

        const matrix =
          GLYPHS[letter];

        for (
          let row = 0;
          row < matrix.length;
          row += 1
        ) {
          for (
            let column = 0;
            column <
            matrix[row].length;
            column += 1
          ) {
            if (
              matrix[row][column] !==
              1
            ) {
              continue;
            }

            points.push({
              x:
                x +
                column *
                  scale *
                  1.5,

              y:
                y +
                row *
                  scale *
                  1.5,
            });
          }
        }

        x +=
          widths[
            letterIndex
          ] +
          letterSpacing;
      }

      return points;
    };

    /*
     * ==================================================
     * PARTICLE TEXT MORPH
     *
     * Number comes in quickly.
     * During disperse it breaks back out.
     * ==================================================
     */

    const drawMorphingPointSet = (
      points: Point[],
      morph: number,
      alpha: number,
      seedOffset: number,
      spreadRadius: number,
    ) => {
      if (
        points.length === 0
      ) {
        return;
      }

      const localMorph =
        smoothstep(morph);

      for (
        let index = 0;
        index < points.length;
        index += 1
      ) {
        const point =
          points[index];

        const seed =
          index +
          seedOffset;

        const angle =
          (
            seed /
            points.length
          ) *
            Math.PI *
            2 +
          Math.sin(
            seed * 11.731,
          ) *
            1.15;

        const radius =
          spreadRadius +
          (
            (
              Math.sin(
                seed * 37.117,
              ) +
              1
            ) /
            2
          ) *
            18;

        const sourceX =
          centerX +
          Math.cos(angle) *
            radius;

        const sourceY =
          centerY +
          58 +
          Math.sin(angle) *
            radius *
            0.32;

        const curve =
          Math.sin(
            localMorph *
              Math.PI,
          );

        const x =
          sourceX +
          (
            point.x -
            sourceX
          ) *
            localMorph +
          Math.cos(
            angle +
              materialPhase *
                0.35,
          ) *
            curve *
            5;

        const y =
          sourceY +
          (
            point.y -
            sourceY
          ) *
            localMorph +
          Math.sin(
            angle +
              materialPhase *
                0.35,
          ) *
            curve *
            3.2;

        const formed =
          Math.pow(
            localMorph,
            1.15,
          );

        const particleAlpha =
          alpha *
          (
            0.15 +
            formed * 0.85
          );

        const hot =
          formed > 0.8 &&
          index % 7 === 0;

        if (hot) {
          drawHotParticle(
            x,
            y,
            0.92,
            particleAlpha,
          );
        } else {
          drawParticle(
            x,
            y,
            0.82 +
              formed * 0.18,
            particleAlpha,
          );
        }
      }
    };

    /*
     * ==================================================
     * FIXED LOOP
     *
     * gather → hold → disperse → pause
     * ==================================================
     */

    type CycleMode =
      | "gather"
      | "hold"
      | "disperse"
      | "pause";

    let animationFrame = 0;

    let previousTimestamp = 0;

    let materialPhase = 0;

    let visibility = 0;

    let progress = 0;

    let disperseProgress = 0;

    let cycleMode: CycleMode =
      "gather";

    let cycleTimer = 0;

    let lastActive = false;

    const resetCycle = () => {
      progress = 0;

      disperseProgress = 0;

      cycleMode =
        "gather";

      cycleTimer = 0;
    };

    const render = (
      timestamp: number,
    ) => {
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

      const active =
        currentState ===
          "thinking" ||
        currentState ===
          "responding";

      if (
        active &&
        !lastActive
      ) {
        resetCycle();
      }

      lastActive = active;

      const visibilityTarget =
        active ? 1 : 0;

      const visibilityResponse =
        1 -
        Math.exp(
          -deltaSeconds * 5,
        );

      visibility +=
        (
          visibilityTarget -
          visibility
        ) *
        visibilityResponse;

      materialPhase +=
        deltaSeconds * 1.3;

      ctx.clearRect(
        0,
        0,
        logicalSize,
        logicalSize,
      );

      if (
        visibility <
        0.004
      ) {
        animationFrame =
          requestAnimationFrame(
            render,
          );

        return;
      }

      /*
       * ==================================================
       * TIMELINE
       *
       * Faster construction.
       * Longer 100% hold.
       * Actual particle disperse instead of fade.
       * ==================================================
       */

      if (
        cycleMode ===
        "gather"
      ) {
        /*
         * About 3 seconds to 100%.
         */
        progress +=
          deltaSeconds / 3;

        progress =
          Math.min(
            1,
            progress,
          );

        if (
          progress >= 1
        ) {
          cycleMode =
            "hold";

          /*
           * Keep 100% clearly visible.
           */
          cycleTimer =
            currentState ===
              "responding"
              ? 2.15
              : 1.85;
        }
      } else if (
        cycleMode ===
        "hold"
      ) {
        progress = 1;

        cycleTimer -=
          deltaSeconds;

        if (
          cycleTimer <= 0
        ) {
          cycleMode =
            "disperse";

          disperseProgress = 0;
        }
      } else if (
        cycleMode ===
        "disperse"
      ) {
        /*
         * Break back into Orb particles.
         */
        disperseProgress +=
          deltaSeconds / 1.15;

        disperseProgress =
          Math.min(
            1,
            disperseProgress,
          );

        if (
          disperseProgress >= 1
        ) {
          cycleMode =
            "pause";

          cycleTimer = 0.35;
        }
      } else {
        cycleTimer -=
          deltaSeconds;

        if (
          cycleTimer <= 0
        ) {
          resetCycle();
        }
      }

      /*
       * ==================================================
       * GLOBAL FORM MORPH
       *
       * gather  : 0 → 1
       * hold    : 1
       * disperse: 1 → 0
       * ==================================================
       */

      let shapeMorph = 0;

      if (
        cycleMode ===
        "gather"
      ) {
        shapeMorph =
          smoothstep(
            progress,
          );
      } else if (
        cycleMode ===
        "hold"
      ) {
        shapeMorph = 1;
      } else if (
        cycleMode ===
        "disperse"
      ) {
        shapeMorph =
          1 -
          smoothstep(
            disperseProgress,
          );
      }

      /*
       * ==================================================
       * MAIN SHAPE
       * ==================================================
       */

      for (
        let index = 0;
        index < particles.length;
        index += 1
      ) {
        const particle =
          particles[index];

        /*
         * Small stable timing variance.
         */
        const delay =
          (
            index %
            17
          ) *
          0.0035;

        let localMorph =
          shapeMorph;

        /*
         * During gather only,
         * keep the organic stagger.
         */
        if (
          cycleMode ===
          "gather"
        ) {
          localMorph =
            Math.max(
              0,
              Math.min(
                1,
                (
                  progress -
                  delay
                ) /
                  (
                    1 -
                    delay
                  ),
              ),
            );

          localMorph =
            smoothstep(
              localMorph,
            );
        }

        const curve =
          Math.sin(
            localMorph *
              Math.PI,
          );

        const angle =
          particle.phase +
          materialPhase *
            0.32;

        const baseX =
          particle.sourceX +
          (
            particle.targetX -
            particle.sourceX
          ) *
            localMorph;

        const baseY =
          particle.sourceY +
          (
            particle.targetY -
            particle.sourceY
          ) *
            localMorph;

        const swirlAmount =
          curve * 10;

        const x =
          baseX +
          Math.cos(angle) *
            swirlAmount;

        const y =
          baseY +
          Math.sin(angle) *
            swirlAmount *
            0.68;

        const formed =
          Math.pow(
            localMorph,
            1.2,
          );

        const alpha =
          visibility *
          (
            0.12 +
            formed * 0.74
          ) *
          particle.brightness;

        const size =
          particle.size *
          (
            0.7 +
            formed * 0.5
          );

        const hot =
          formed > 0.82 &&
          particle.brightness >
            0.9;

        if (
          hot &&
          index % 11 === 0
        ) {
          drawHotParticle(
            x,
            y,
            size,
            alpha,
          );
        } else {
          drawParticle(
            x,
            y,
            size,
            alpha,
            hot,
          );
        }
      }

      /*
       * ==================================================
       * PROGRESS VALUE
       *
       * Faster arrival than main shape.
       * ==================================================
       */

      const progressValue =
        Math.round(
          progress * 100,
        );

      /*
       * Number is already mostly formed
       * when task reaches ~30%.
       */
      const numberGatherMorph =
        smoothstep(
          Math.max(
            0,
            Math.min(
              1,
              progress / 0.32,
            ),
          ),
        );

      let numberMorph = 0;

      if (
        cycleMode ===
        "gather"
      ) {
        numberMorph =
          numberGatherMorph;
      } else if (
        cycleMode ===
        "hold"
      ) {
        numberMorph = 1;
      } else if (
        cycleMode ===
        "disperse"
      ) {
        /*
         * Number stays longer,
         * then breaks apart.
         */
        const delayedDisperse =
          Math.max(
            0,
            (
              disperseProgress -
              0.18
            ) /
              0.82,
          );

        numberMorph =
          1 -
          smoothstep(
            delayedDisperse,
          );
      }

      /*
       * ==================================================
       * THINKING
       * ==================================================
       */

      if (
        currentState ===
        "thinking"
      ) {
        const numberPoints =
          createNumberPoints(
            progressValue,
          );

        drawMorphingPointSet(
          numberPoints,
          numberMorph,
          visibility * 0.94,
          700,
          36,
        );
      }

      /*
       * ==================================================
       * RESPONDING
       *
       * Percentage until completed.
       * Then DONE / FAILED.
       * ==================================================
       */

      if (
        currentState ===
        "responding"
      ) {
        if (
          progress >= 1 &&
          (
            cycleMode ===
              "hold" ||
            cycleMode ===
              "disperse"
          ) &&
          resultRef.current
        ) {
          const resultPoints =
            createResultPoints(
              resultRef.current,
            );

          drawMorphingPointSet(
            resultPoints,
            numberMorph,
            visibility * 0.96,
            1300,
            38,
          );
        } else {
          const numberPoints =
            createNumberPoints(
              progressValue,
            );

          drawMorphingPointSet(
            numberPoints,
            numberMorph,
            visibility * 0.94,
            700,
            36,
          );
        }
      }

      /*
       * ==================================================
       * COMPLETED SHAPE ENERGY
       *
       * Very subtle only.
       * ==================================================
       */

      if (
        cycleMode ===
        "hold"
      ) {
        for (
          let index = 0;
          index < particles.length;
          index += 19
        ) {
          const particle =
            particles[index];

          const pulse =
            (
              Math.sin(
                materialPhase *
                  1.65 +
                  particle.phase,
              ) +
              1
            ) /
            2;

          if (
            pulse < 0.72
          ) {
            continue;
          }

          drawHotParticle(
            particle.targetX,
            particle.targetY,
            0.72 +
              pulse * 0.35,
            visibility *
              pulse *
              0.34,
          );
        }
      }

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
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      style={{
        position: "absolute",
        left: "50%",
        top: "50%",
        transform:
          "translate(-50%, -50%)",
        zIndex: 6,
        pointerEvents: "none",
        width: "248px",
        height: "248px",
      }}
    />
  );
}

export default OrbTaskRenderer;