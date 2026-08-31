import type { SVGProps } from "react";

/**
 * Small inline vector icons for the task console action surfaces. Kept as inline
 * SVG (no icon dependency) so they inherit `currentColor` and design tokens.
 */
function svgProps(props: SVGProps<SVGSVGElement>): SVGProps<SVGSVGElement> {
  return {
    width: 16,
    height: 16,
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
    ...props,
  };
}

export function IconOpen(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...svgProps(props)}>
      <path d="M9.5 2.5h4v4" />
      <path d="M13.5 2.5 8 8" />
      <path d="M11 9.5V12a1.5 1.5 0 0 1-1.5 1.5h-6A1.5 1.5 0 0 1 2 12V6a1.5 1.5 0 0 1 1.5-1.5H6" />
    </svg>
  );
}

export function IconDownload(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...svgProps(props)}>
      <path d="M8 2.5v7" />
      <path d="M4.75 6.75 8 10l3.25-3.25" />
      <path d="M3 13h10" />
    </svg>
  );
}

export function IconChevronDown(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...svgProps(props)}>
      <path d="m4 6 4 4 4-4" />
    </svg>
  );
}

export function IconDots(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...svgProps(props)} strokeWidth={0} fill="currentColor">
      <circle cx="3.5" cy="8" r="1.3" />
      <circle cx="8" cy="8" r="1.3" />
      <circle cx="12.5" cy="8" r="1.3" />
    </svg>
  );
}

export function IconSelection(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...svgProps(props)} strokeDasharray="2.5 2">
      <rect x="2.5" y="2.5" width="11" height="11" rx="1.5" />
    </svg>
  );
}
