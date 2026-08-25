import "./LivingTelemetryField.css";

type Metric = {
  label: string;
  value: string;
  detail: string;
  level: number;
};

type Drive = {
  id: string;
  name: string;
  mount: string;
  usedGb: number;
  totalGb: number;
  kind: string;
};

type SystemHealth =
  | "stable"
  | "warning"
  | "critical";

const currentSystemHealth: SystemHealth =
  "stable";

const systemHealthLabels: Record<
  SystemHealth,
  string
> = {
  stable: "SYSTEM STABLE",
  warning: "SYSTEM WARNING",
  critical: "SYSTEM CRITICAL",
};

const metrics: Metric[] = [
  {
    label: "CPU",
    value: "18%",
    detail: "6C / 12T",
    level: 18,
  },
  {
    label: "RAM",
    value: "41%",
    detail: "13.1 / 32 GB",
    level: 41,
  },
  {
    label: "GPU",
    value: "07%",
    detail: "RTX 3070 Ti",
    level: 7,
  },
  {
    label: "TEMP",
    value: "47°",
    detail: "SYSTEM",
    level: 47,
  },
];

const drives: Drive[] = [
  {
    id: "c",
    name: "SYSTEM",
    mount: "C:",
    usedGb: 314,
    totalGb: 476,
    kind: "NVMe",
  },
  {
    id: "e",
    name: "PROJECTS",
    mount: "E:",
    usedGb: 512,
    totalGb: 931,
    kind: "SSD",
  },
  {
    id: "f",
    name: "EXTERNAL",
    mount: "F:",
    usedGb: 81,
    totalGb: 238,
    kind: "USB",
  },
];

function LivingTelemetryField() {
  const getUsedPercent = (
    drive: Drive,
  ) => {
    if (drive.totalGb <= 0) {
      return 0;
    }

    return Math.round(
      (drive.usedGb /
        drive.totalGb) *
        100,
    );
  };

  const getFreeGb = (
    drive: Drive,
  ) =>
    Math.max(
      0,
      drive.totalGb -
        drive.usedGb,
    );

  return (
    <aside
      className="system-constellation"
      dir="ltr"
      aria-label="System and storage telemetry"
    >
      <section className="constellation-system">
        <header className="constellation-header">
          <span className="constellation-kicker">
            SYSTEM
          </span>

          <div
            className="system-live-signal"
            aria-hidden="true"
          >
            <i />
            <i />
            <i />
          </div>
        </header>

        <div className="system-grid">
          {metrics.map(
            (
              metric,
              index,
            ) => (
              <article
                key={metric.label}
                className={`system-metric system-metric-${index + 1}`}
              >
                <span className="metric-hover-bloom" />

                <span
                  className="metric-particle metric-particle-a"
                  aria-hidden="true"
                />

                <span
                  className="metric-particle metric-particle-b"
                  aria-hidden="true"
                />

                <span
                  className="metric-particle metric-particle-c"
                  aria-hidden="true"
                />

                <div className="metric-main">
                  <span className="metric-label">
                    {metric.label}
                  </span>

                  <strong>
                    {metric.value}
                  </strong>
                </div>

                <span className="metric-detail">
                  {metric.detail}
                </span>

                <div
                  className="metric-energy"
                  aria-hidden="true"
                >
                  <span
                    style={{
                      width:
                        `${Math.max(
                          10,
                          metric.level,
                        )}%`,
                    }}
                  />

                  <i />
                </div>
              </article>
            ),
          )}
        </div>

        <div
          className="system-status"
          data-health={
            currentSystemHealth
          }
        >
          <span className="system-status-core">
            <i />
          </span>

          <span>
            {
              systemHealthLabels[
                currentSystemHealth
              ]
            }
          </span>
        </div>
      </section>

      <div
        className="constellation-separator"
        aria-hidden="true"
      >
        <span />
        <i />
        <span />
      </div>

      <section className="constellation-storage">
        <header className="storage-header">
          <span className="constellation-kicker">
            STORAGE
          </span>

          <span className="storage-live">
            <i />
            LIVE
          </span>
        </header>

        <div className="storage-grid">
          {drives.map(
            (drive) => {
              const used =
                getUsedPercent(
                  drive,
                );

              const gradientId =
                `storage-flow-${drive.id}`;

              return (
                <button
                  type="button"
                  className="volume-node"
                  key={drive.id}
                  title={`${drive.mount} ${drive.name}`}
                >
                  <span className="volume-hover-bloom" />

                  <span className="volume-orbit">
                    <svg
                      viewBox="0 0 52 52"
                      aria-hidden="true"
                    >
                      <defs>
                        <linearGradient
                          id={gradientId}
                          x1="6"
                          y1="26"
                          x2="46"
                          y2="26"
                          gradientUnits="userSpaceOnUse"
                        >
                          <stop
                            offset="0%"
                            stopColor="rgba(74, 190, 229, 0.42)"
                          />

                          <stop
                            offset="27%"
                            stopColor="rgba(104, 224, 255, 0.72)"
                          />

                          <stop
                            offset="48%"
                            stopColor="rgba(228, 254, 255, 1)"
                          />

                          <stop
                            offset="64%"
                            stopColor="rgba(125, 232, 255, 0.9)"
                          />

                          <stop
                            offset="100%"
                            stopColor="rgba(74, 190, 229, 0.4)"
                          />

                          <animateTransform
                            attributeName="gradientTransform"
                            type="rotate"
                            from="0 26 26"
                            to="360 26 26"
                            dur="3.4s"
                            repeatCount="indefinite"
                          />
                        </linearGradient>
                      </defs>

                      <circle
                        className="volume-ring-background"
                        cx="26"
                        cy="26"
                        r="20"
                        pathLength="100"
                      />

                      <circle
                        className="volume-ring-value"
                        cx="26"
                        cy="26"
                        r="20"
                        pathLength="100"
                        stroke={`url(#${gradientId})`}
                        strokeDasharray={`${used} ${100 - used}`}
                      />
                    </svg>

                    <span className="volume-core-dot" />
                  </span>

                  <span className="volume-info">
                    <span className="volume-title-row">
                      <strong>
                        {drive.mount}
                      </strong>

                      <span>
                        {used}%
                      </span>
                    </span>

                    <small>
                      {getFreeGb(
                        drive,
                      )}{" "}
                      GB FREE
                    </small>

                    <span className="volume-name">
                      {drive.name}
                    </span>
                  </span>

                  <span className="volume-kind">
                    {drive.kind}
                  </span>
                </button>
              );
            },
          )}

          <button
            type="button"
            className="volume-node qronos-volume-node"
            title="Qronos managed storage"
          >
            <span className="volume-hover-bloom" />

            <span className="qronos-storage-orbit">
              <span
                className="qronos-pulse-wave qronos-pulse-wave-1"
                aria-hidden="true"
              />

              <span
                className="qronos-pulse-wave qronos-pulse-wave-2"
                aria-hidden="true"
              />

              <svg
                viewBox="0 0 52 52"
                aria-hidden="true"
              >
                <circle
                  className="qronos-storage-ring qronos-storage-ring-a"
                  cx="26"
                  cy="26"
                  r="20"
                  pathLength="100"
                />

                <circle
                  className="qronos-storage-ring qronos-storage-ring-b"
                  cx="26"
                  cy="26"
                  r="15"
                  pathLength="100"
                />
              </svg>

              <i className="qronos-storage-particle qsp-1" />
              <i className="qronos-storage-particle qsp-2" />
              <i className="qronos-storage-particle qsp-3" />

              <span className="qronos-storage-core" />
            </span>

            <span className="volume-info">
              <span className="volume-title-row">
                <strong>
                  QRONOS
                </strong>

                <span>
                  6.8 GB
                </span>
              </span>

              <small>
                MANAGED STORAGE
              </small>

              <span className="qronos-storage-health">
                <i />
                HEALTHY
              </span>
            </span>
          </button>
        </div>
      </section>
    </aside>
  );
}

export default LivingTelemetryField;