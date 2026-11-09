"use client";

import { useState } from "react";
import { club, type Club } from "@/lib/teams";

const SIZE = {
  sm: "h-8 w-8 text-[11px]",
  md: "h-14 w-14 text-lg",
  lg: "h-20 w-20 text-2xl",
} as const;

export function TeamMark({
  abbr,
  team: given,
  size = "md",
}: {
  abbr?: string;
  team?: Club;
  size?: keyof typeof SIZE;
}) {
  const team = given ?? club(abbr);
  const [failed, setFailed] = useState(!team.logo);

  return (
    <span
      className={`relative grid shrink-0 place-items-center overflow-hidden bg-ink-950 ${SIZE[size]}`}
      style={{ boxShadow: `inset 0 0 0 1.5px ${team.primary}` }}
      title={`${team.city} ${team.nick}`}
    >
      {failed ? (
        <span className="font-display uppercase leading-none tracking-tight" style={{ color: team.primary }}>
          {team.abbr.slice(0, 3)}
        </span>
      ) : (
        // Club marks are served by the league art host. The monogram stands in if that request fails.
        <img
          src={team.logo}
          alt=""
          referrerPolicy="no-referrer"
          className="h-[78%] w-[78%] object-contain"
          onError={() => setFailed(true)}
        />
      )}
      <span className="absolute inset-x-0 bottom-0 h-[3px]" style={{ background: team.secondary }} />
    </span>
  );
}
