import type { Config } from "tailwindcss";

/**
 * Site palette. Club colors do not live here.
 * void / ink  — surfaces
 * paper / mute — type
 * gold — brand lime. The token is named gold so it does not collide with Tailwind's built-in lime scale.
 * signal — fluorescent blue. College marks and the "at" between clubs.
 * flare — fluorescent pink. Anytime TD.
 * heat — fluorescent orange. Misses and missing data.
 * ice — same fluorescent blue, kept so older FBS classes stay on the palette.
 * gold, field, and danger are aliases so older classes stay on this palette.
 */
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#07080C",
          900: "#0C1018",
          800: "#141A24",
          700: "#1C2533",
          600: "#2C3848",
        },
        paper: "#F4F6FB",
        mute: "#8E97A8",
        gold: "#D6FF3F",
        field: "#D6FF3F",
        signal: "#3AF2FF",
        flare: "#FF4BD8",
        heat: "#FF5C39",
        danger: "#FF5C39",
        ice: "#3AF2FF",
      },
      fontFamily: {
        display: ["var(--font-display)", "Impact", "sans-serif"],
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      boxShadow: {
        panel: "0 24px 60px rgba(0,0,0,0.45)",
      },
    },
  },
  plugins: [],
};

export default config;
