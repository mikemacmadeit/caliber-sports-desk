const KEY = "caliber-nav-stack";

function read(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = sessionStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function write(stack: string[]) {
  sessionStorage.setItem(KEY, JSON.stringify(stack.slice(-40)));
}

/** Record the page the desk is showing. A browser Back pops the stack instead of adding a duplicate. */
export function rememberNavigation(href: string) {
  const stack = read();
  if (stack[stack.length - 1] === href) return;
  if (stack[stack.length - 2] === href) stack.pop();
  else stack.push(href);
  write(stack);
}

/** The in-app page under this one. Skips the current href when it is already on the stack. */
export function previousPage(currentHref?: string): string | null {
  const stack = read();
  for (let i = stack.length - 1; i >= 0; i -= 1) {
    if (stack[i] !== currentHref) return stack[i];
  }
  return null;
}

export function pageLabel(href: string) {
  const path = href.split("?")[0] || "/";
  if (path === "/") return "Board";
  if (path.startsWith("/best-bets")) return "Best bets";
  if (path.startsWith("/ats")) return "ATS";
  if (path.startsWith("/anytime-td")) return "Anytime TD";
  if (path.startsWith("/props")) return "Props";
  if (path.startsWith("/scenario")) return "Scenario";
  if (path.startsWith("/parlays")) return "Parlays";
  if (path.startsWith("/track-record")) return "Ledger";
  if (path.startsWith("/data-quality")) return "Health";
  if (path.startsWith("/games/")) return "Game";
  return "Back";
}
