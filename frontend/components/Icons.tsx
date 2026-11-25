import type { SVGProps } from "react";

const PATHS: Record<string, string> = {
  check: "M3 8.5l3.2 3.2L13 4.8",
  x: "M4 4l8 8M12 4l-8 8",
  eye: "M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8zM8 10a2 2 0 100-4 2 2 0 000 4z",
  clock: "M8 14A6 6 0 108 2a6 6 0 000 12zM8 4.8V8l2.2 1.4",
  hourglass: "M4 2.5h8M4 13.5h8M5 2.5c0 3 3 3.5 3 5.5s-3 2.5-3 5.5M11 2.5c0 3-3 3.5-3 5.5s3 2.5 3 5.5",
  minus: "M4 8h8",
  alert: "M8 2.5L14 13H2L8 2.5zM8 6.5v3M8 11.3v.1",
  external: "M9 3h4v4M13 3L7.5 8.5M11 9.5V13H3V5h3.5",
  copy: "M5.5 5.5h7v7h-7zM3.5 10.5v-7h7",
  pause: "M5.5 3.5v9M10.5 3.5v9",
  play: "M5 3.5l7 4.5-7 4.5z",
  pulse: "M1.5 8h3l1.5-4 3 8 1.5-4h4",
};

export function Icon({ name, ...rest }: { name: keyof typeof PATHS | string } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      <path d={PATHS[name] ?? PATHS.minus} />
    </svg>
  );
}

export const STATUS_ICON: Record<string, string> = {
  eligible: "check",
  observing: "eye",
  pending_data: "clock",
  rejected: "x",
  expired: "hourglass",
};

export const OUTCOME_ICON: Record<string, string> = {
  pass: "check",
  fail: "x",
  pending: "clock",
  skipped: "minus",
};
