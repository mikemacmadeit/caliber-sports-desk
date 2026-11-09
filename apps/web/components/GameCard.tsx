import Link from "next/link";
import { Freshness, LeagueBadge, TierChip } from "@/components/StatusChips";
import { TeamMark } from "@/components/TeamMark";
import type { SlateGame } from "@/lib/api";
import { cToF, kickoffParts, mpsToMph } from "@/lib/format";
import { presentTeam } from "@/lib/teams";

export function GameCard({ game, featured = false }: { game: SlateGame; featured?: boolean }) {
  const away = presentTeam(game.away);
  const home = presentTeam(game.home);
  const when = kickoffParts(game.kickoff_utc);
  const rain =
    game.weather?.precip_probability != null && game.weather.precip_probability >= 0.3
      ? ` · ${Math.round(game.weather.precip_probability * 100)}% rain`
      : game.weather?.precip_mm && game.weather.precip_mm > 0
        ? " · wet"
        : "";
  const alerts = game.weather?.alerts?.length ? ` · ${game.weather.alerts.length} alert` : "";
  const observed = game.weather?.quality_status === "observed_not_forecast";
  return (
    <Link
      href={`/games/${game.game_id}`}
      className={`plate group block overflow-hidden ${featured ? "p-6 md:p-8" : "p-5"}`}
    >
      <span aria-hidden className="absolute bottom-0 left-0 top-0 w-[3px]" style={{ background: away.primary }} />
      <span aria-hidden className="absolute bottom-0 right-0 top-0 w-[3px]" style={{ background: home.primary }} />
      <span
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background: `linear-gradient(105deg, ${away.primary}${featured ? "33" : "1c"} 0%, transparent 38%, transparent 62%, ${home.primary}${featured ? "33" : "1c"} 100%)`,
        }}
      />

      <div className="relative">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            {featured ? (
              <span className={game.status === "in_progress" ? "kicker kicker-signal" : "kicker"}>
                {game.status === "in_progress" ? "Live" : "Next kickoff"}
              </span>
            ) : null}
            {game.status === "in_progress" && !featured ? (
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-signal">Live</span>
            ) : null}
            <LeagueBadge league={game.league_id} />
            <TierChip tier={game.data_tier} />
            {game.conference ? (
              <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{game.conference}</span>
            ) : null}
          </div>
          <Freshness minutes={game.model_freshness_minutes} />
        </div>

        {featured ? (
          <div className="mt-6 flex flex-wrap items-center gap-x-10 gap-y-4">
            <div className="flex items-center gap-4">
              <TeamMark team={away} size="lg" />
              <TeamSide abbr={away.abbr} nick={away.nick} featured />
            </div>
            <div className="font-mono text-[10px] uppercase tracking-[0.28em] text-signal">at</div>
            <div className="flex items-center gap-4">
              <TeamMark team={home} size="lg" />
              <TeamSide abbr={home.abbr} nick={home.nick} featured />
            </div>
          </div>
        ) : (
          <div className="mt-5 grid grid-cols-[auto_1fr_auto_1fr_auto] items-center gap-3">
            <TeamMark team={away} />
            <TeamSide abbr={away.abbr} nick={away.nick} />
            <div className="font-mono text-[10px] uppercase tracking-[0.28em] text-signal">at</div>
            <TeamSide abbr={home.abbr} nick={home.nick} align="right" />
            <TeamMark team={home} />
          </div>
        )}

        <div className="mt-5 flex flex-col gap-3 border-t border-white/10 pt-3 sm:flex-row sm:items-end sm:justify-between">
          <div className="grid min-w-0 flex-1 grid-cols-3 gap-3">
            <Fact label="Kick" value={when.day} detail={when.time} />
            <Fact label="Venue" value={shortVenue(game.venue.name)} detail={`${game.venue.surface} · ${game.venue.roof_type}`} />
            <Fact
              label="Air"
              value={game.weather ? cToF(game.weather.temperature_c) : "—"}
              detail={
                !game.weather
                  ? "No read"
                  : observed
                    ? "Observed, not a forecast"
                    : `${mpsToMph(game.weather.wind_mps)}${rain}${alerts}`
              }
            />
          </div>
          <span className="self-end font-display text-xl uppercase tracking-wide text-gold">Open →</span>
        </div>
        {game.research?.headline ? (
          <p className="mt-3 font-mono text-[10px] uppercase tracking-wider text-gold">
            Research · {game.research.headline}
          </p>
        ) : null}
      </div>
    </Link>
  );
}

function TeamSide({
  abbr,
  nick,
  align = "left",
  featured = false,
}: {
  abbr: string;
  nick: string;
  align?: "left" | "right";
  featured?: boolean;
}) {
  return (
    <div className={`min-w-0 ${align === "right" ? "text-right" : ""}`}>
      <div
        className={`truncate font-display uppercase leading-none tracking-tight text-paper ${
          featured ? "text-6xl sm:text-8xl" : "text-5xl sm:text-6xl"
        }`}
      >
        {abbr}
      </div>
      <div className="mt-1 truncate text-sm text-mute">{nick}</div>
    </div>
  );
}

function shortVenue(name: string) {
  return name.replace(/^GEHA Field at /i, "").replace(/\s+(Stadium|Field)$/i, "");
}

function Fact({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="min-w-0">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">{label}</div>
      <div className="mt-1 line-clamp-2 text-sm leading-snug text-paper">{value}</div>
      <div className="truncate font-mono text-[10px] uppercase tracking-wider text-mute">{detail}</div>
    </div>
  );
}
