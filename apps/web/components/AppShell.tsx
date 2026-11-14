"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { rememberNavigation } from "@/lib/navHistory";

const GROUPS = [
  {
    label: "The slate",
    items: [
      { href: "/", label: "Board", hint: "Every game, in kick order" },
      { href: "/best-bets", label: "Best bets", hint: "Strongest calls for the day" },
      { href: "/parlays", label: "Parlays", hint: "Tickets for the day" },
      { href: "/ats", label: "ATS", hint: "One side of the number" },
      { href: "/anytime-td", label: "Anytime TD", hint: "Who finds the end zone" },
      { href: "/props", label: "Props", hint: "Receptions, rush, receiving" },
    ],
  },
  {
    label: "Run a number",
    items: [
      { href: "/scenario", label: "Scenario", hint: "Change the weather, rerun" },
    ],
  },
  {
    label: "The record",
    items: [
      { href: "/track-record", label: "Ledger", hint: "Calls that stay on the books" },
      { href: "/data-quality", label: "Health", hint: "Whether the model is ready" },
    ],
  },
];

const NAV = GROUPS.flatMap((group) => group.items);
const PRIMARY = GROUPS[0].items;
const DESK = GROUPS.slice(1).flatMap((group) => group.items);

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const date = searchParams.get("date");
  const search = searchParams.toString();
  const here = NAV.find((item) => isActive(pathname, item.href));
  // Record during render so a click cannot leave before this page is on the stack.
  if (typeof window !== "undefined") {
    rememberNavigation(search ? `${pathname}?${search}` : pathname);
  }

  // Main nav always opens "today". The day strip is what moves you to another date.
  function hrefFor(href: string) {
    void date;
    return href;
  }

  return (
    <div className="min-h-screen min-w-0 lg:pl-64">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col overflow-hidden border-r border-white/10 bg-ink-950/95 backdrop-blur lg:flex">
        <Link href="/" className="flex items-center gap-3 px-5 pb-5 pt-6">
          <span className="grid h-10 w-10 place-items-center bg-gold font-display text-2xl leading-none text-ink-950">
            C
          </span>
          <span>
            <span className="block font-display text-3xl uppercase leading-none tracking-wide text-paper">Caliber</span>
            <span className="mt-1 block font-mono text-[10px] uppercase tracking-[0.28em] text-mute">Probability desk</span>
          </span>
        </Link>
        <nav className="flex flex-1 flex-col gap-5 overflow-y-auto px-3">
          {GROUPS.map((group) => (
            <div key={group.label}>
              <p className="px-3 pb-1.5 font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{group.label}</p>
              <div className="flex flex-col gap-0.5">
                {group.items.map((item) => {
                  const active = isActive(pathname, item.href);
                  return (
                    <Link
                      key={item.href}
                      href={hrefFor(item.href)}
                      aria-current={active ? "page" : undefined}
                      className={`px-3 py-2 transition ${
                        active ? "bg-gold text-ink-950" : "text-paper/80 hover:bg-white/5 hover:text-paper"
                      }`}
                    >
                      <span
                        className={`block font-display uppercase leading-none tracking-wide ${
                          group.label === "The slate" ? "text-xl" : "text-lg text-paper/70"
                        } ${active ? "text-ink-950" : ""}`}
                      >
                        {item.label}
                      </span>
                      {active ? (
                        <span className="mt-1 block text-[11px] font-medium normal-case tracking-normal text-ink-950/70">
                          {item.hint}
                        </span>
                      ) : null}
                    </Link>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>
        <div className="px-5 py-5">
          <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.22em] text-mute">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-gold" />
            Desk live
          </div>
          <p className="mt-3 text-[11px] leading-relaxed text-mute">
            Research only. Not a sportsbook. NFL and college are scored apart.
          </p>
        </div>
      </aside>

      <div className="sticky top-0 z-20 border-b border-white/10 bg-ink-950/95 pt-[env(safe-area-inset-top)] backdrop-blur lg:hidden">
        <div className="flex items-center justify-between px-4 py-3">
          <Link href="/" className="flex min-h-11 items-center gap-2">
            <span className="grid h-8 w-8 place-items-center bg-gold font-display text-xl leading-none text-ink-950">C</span>
            <span className="font-display text-2xl uppercase tracking-wide">Caliber</span>
          </Link>
          <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-gold">Live</span>
        </div>
        <nav aria-label="Sections" className="flex flex-wrap gap-1 px-3 pb-2">
          {PRIMARY.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={hrefFor(item.href)}
                aria-current={active ? "page" : undefined}
                className={`inline-flex min-h-11 items-center px-3 font-display text-lg uppercase tracking-wide ${
                  active ? "bg-gold text-ink-950" : "text-paper/70"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <nav aria-label="Desk" className="flex flex-wrap items-center gap-1 px-3 pb-3">
          <span className="px-2 font-mono text-[10px] uppercase tracking-[0.18em] text-mute">Desk</span>
          {DESK.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={hrefFor(item.href)}
                aria-current={active ? "page" : undefined}
                className={`inline-flex min-h-11 items-center px-3 font-display text-base uppercase tracking-wide ${
                  active ? "bg-gold text-ink-950" : "text-paper/45"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>

      <div className="hidden border-b border-white/10 bg-ink-900/70 md:block">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-2.5 text-[13px] text-paper/70 lg:px-10">
          <span
            className={`font-mono text-[10px] uppercase tracking-[0.22em] ${
              pathname.startsWith("/anytime-td")
                ? "text-flare"
                : pathname.startsWith("/props")
                  ? "text-signal"
                  : "text-gold"
            }`}
          >
            {here?.label ?? "Caliber"}
          </span>
          <span className="text-white/25">/</span>
          {here?.hint ?? "Research desk"}
          <span className="text-white/25">/</span>
          {pathname === "/"
            ? "The win, the number, the tickets"
            : pathname.startsWith("/best-bets")
              ? "Today's card"
              : pathname.startsWith("/parlays")
                ? "Tickets for the day"
                : pathname.startsWith("/ats")
                  ? "One call per game"
                  : pathname.startsWith("/track-record")
                    ? "What the calls did"
                    : pathname.startsWith("/data-quality")
                      ? "The lab"
                      : "The slate"}
        </p>
      </div>

      <main className="min-w-0 px-4 py-6 pb-[max(1.5rem,env(safe-area-inset-bottom))] sm:px-5 sm:py-8 lg:px-10 lg:py-10">{children}</main>
    </div>
  );
}
