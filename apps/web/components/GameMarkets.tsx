"use client";

import { useMemo, useState } from "react";
import { PropTable } from "@/components/PropTable";
import type { PropRow } from "@/lib/api";
import { marketLabel } from "@/lib/format";

const ORDER = ["anytime_td", "rushing_yards", "receiving_yards", "receptions"];

export function GameMarkets({ rows }: { rows: PropRow[] }) {
  const groups = useMemo(() => {
    const map = new Map<string, PropRow[]>();
    for (const row of rows) {
      const list = map.get(row.market_id) || [];
      list.push(row);
      map.set(row.market_id, list);
    }
    return [...map.entries()].sort((a, b) => {
      const ai = ORDER.indexOf(a[0]);
      const bi = ORDER.indexOf(b[0]);
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
  }, [rows]);

  const [active, setActive] = useState<string>(groups[0]?.[0] || "all");
  const visible = active === "all" ? rows : groups.find(([id]) => id === active)?.[1] || [];

  if (!rows.length) {
    return (
      <div className="plate px-5 py-8 text-sm text-mute">No priced player markets on this game yet.</div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <Tab active={active === "all"} onClick={() => setActive("all")} label={`All · ${rows.length}`} />
        {groups.map(([id, list]) => (
          <Tab
            key={id}
            active={active === id}
            onClick={() => setActive(id)}
            label={`${marketLabel(id)} · ${list.length}`}
          />
        ))}
      </div>
      <PropTable rows={visible} />
    </div>
  );
}

function Tab({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`border px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.16em] transition ${
        active
          ? "border-gold bg-gold text-ink-950"
          : "border-white/15 text-mute hover:border-white/30 hover:text-paper"
      }`}
    >
      {label}
    </button>
  );
}
