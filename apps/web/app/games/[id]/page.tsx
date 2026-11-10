import { BackLink } from "@/components/BackLink";
import { GameMarkets } from "@/components/GameMarkets";
import { PickPair } from "@/components/CallCard";
import { MarginBar } from "@/components/MarginBar";
import { ModelGaps } from "@/components/ModelGaps";
import { ResearchPanel } from "@/components/ResearchPanel";
import { LeagueBadge, TierChip } from "@/components/StatusChips";
import { TeamMark } from "@/components/TeamMark";
import { TodayPropPicks } from "@/components/TodayPropPicks";
import { UsagePanel } from "@/components/UsagePanel";
import { apiGet, type GameDetail } from "@/lib/api";
import { cToF, kickoff, kickoffDate, mpsToMph } from "@/lib/format";
import { recommendationCopy, reasonLabel } from "@/lib/labels";
import { presentTeam } from "@/lib/teams";

export default async function GamePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const data = await apiGet<GameDetail>(`/v1/games/${id}`);
  const g = data.game;
  const away = presentTeam(g.away);
  const home = presentTeam(g.home);
  const card = await loadCard(g.league_id, g.game_id);
  const explanation = data.research?.explanations?.[0];
  const call = recommendationCopy(explanation?.recommendation_status);
  const weatherTitle =
    data.weather?.quality_status === "observed_not_forecast" ? "Observed weather" : "Kickoff weather";

  return (
    <div className="space-y-10">
      <div>
        <BackLink />
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <LeagueBadge league={g.league_id} />
          <TierChip tier={g.data_tier} />
          <span className="font-mono text-[11px] uppercase tracking-wider text-mute">
            {kickoff(g.kickoff_utc)}
          </span>
        </div>

        <div className="mt-6 flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-8 sm:gap-y-5">
          <div className="flex items-center gap-4">
            <TeamMark team={away} size="lg" />
            <div>
              <div className="font-display text-5xl uppercase leading-none tracking-tight sm:text-8xl">
                {away.abbr}
              </div>
              <div className="mt-2 text-sm text-mute">
                {away.city} {away.nick}
              </div>
            </div>
          </div>
          <div className="font-mono text-[11px] uppercase tracking-[0.32em] text-signal">at</div>
          <div className="flex items-center gap-4">
            <TeamMark team={home} size="lg" />
            <div>
              <div className="font-display text-5xl uppercase leading-none tracking-tight sm:text-8xl">
                {home.abbr}
              </div>
              <div className="mt-2 text-sm text-mute">
                {home.city} {home.nick}
              </div>
            </div>
          </div>
        </div>
        <div className="mt-5 flex h-1 max-w-xl overflow-hidden">
          <div className="flex-1" style={{ background: away.primary }} />
          <div className="flex-1" style={{ background: home.primary }} />
        </div>
        {card ? <CardReadout row={card} awayColor={away.primary} homeColor={home.primary} /> : null}
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-paper/75">
          {weatherLine(data.weather)} {injuryLine(data.injuries)}
        </p>
        {String(g.status || "").toLowerCase() === "final" ? null : (
          <div className="mt-4 max-w-2xl">
            <TodayPropPicks league={g.league_id} date={kickoffDate(g.kickoff_utc)} gameId={g.game_id} />
          </div>
        )}
        <div className="mt-4 flex flex-wrap gap-2">
          <Fact>{g.venue.name}</Fact>
          <Fact>{g.venue.surface}</Fact>
          <Fact>{g.venue.roof_type}</Fact>
          {g.conference ? <Fact>{g.conference}</Fact> : null}
        </div>
      </div>

      <details className="plate p-5 sm:p-6">
        <summary className="cursor-pointer font-display text-3xl uppercase leading-none tracking-wide">
          The lab
        </summary>
        <p className="mt-3 max-w-2xl text-sm text-mute">
          Weather, who is out, and where the model and the price disagree. The call above is the published number.
        </p>
      <section
        className={`mt-6 border p-5 sm:p-6 ${
          call.tone === "play" ? "border-gold/40" : call.tone === "demo" ? "border-ice/40" : "border-heat/25"
        }`}
      >
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div className="max-w-2xl">
            <p className="kicker">Game call</p>
            <p
              className={`mt-2 font-display text-5xl uppercase leading-none tracking-wide sm:text-6xl ${
                call.tone === "play" ? "text-gold" : call.tone === "demo" ? "text-ice" : "text-heat"
              }`}
            >
              {call.title}
            </p>
            <p className="mt-3 text-sm leading-relaxed text-paper/80">
              {explanation?.narrative
                ? firstSentence(explanation.narrative)
                : "Open the research brief below for the full read. Probabilities stay on the official number."}
            </p>
          </div>
          <div className="grid min-w-[12rem] gap-2 text-right font-mono text-[11px] uppercase tracking-wider text-mute">
            <span>{data.props.length} player markets</span>
            <span>{data.model_disagreement.length} model–market gaps</span>
            <span>
              {data.injuries.length
                ? `${data.injuries.length} injury row${data.injuries.length === 1 ? "" : "s"}`
                : "Injury coverage incomplete"}
            </span>
          </div>
        </div>
        {explanation?.reason_codes?.length ? (
          <ul className="mt-5 flex flex-wrap gap-2">
            {explanation.reason_codes.map((code) => (
              <li
                key={code}
                className="border border-white/10 bg-black/25 px-2.5 py-1.5 text-xs leading-snug text-paper/80"
              >
                {reasonLabel(code)}
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title={weatherTitle} hint="How the field should play">
          {data.weather ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-end gap-4">
                <div className="font-display text-5xl uppercase leading-none text-paper">
                  {cToF(data.weather.temperature_c)}
                </div>
                <div>
                  <div className="text-base text-paper">{data.weather.summary || "Conditions on file"}</div>
                  <div className="mt-1 font-mono text-[11px] uppercase tracking-wider text-mute">
                    Roof {data.weather.roof_status}
                  </div>
                </div>
              </div>
              <dl className="grid grid-cols-2 gap-3 text-sm">
                <Metric label="Wind" value={mpsToMph(data.weather.wind_mps)} />
                <Metric label="Gust" value={mpsToMph(data.weather.gust_mps)} />
                <Metric
                  label="Precip"
                  value={
                    data.weather.precip_mm != null
                      ? `${data.weather.precip_mm} mm`
                      : data.weather.precip_probability != null
                        ? `${Math.round(data.weather.precip_probability * 100)}% chance`
                        : "—"
                  }
                />
                <Metric
                  label="Source"
                  value={
                    data.weather.quality_status === "nws_forecast"
                      ? "NWS forecast"
                      : data.weather.quality_status === "observed_not_forecast"
                        ? "Gamebook obs."
                        : "On file"
                  }
                />
              </dl>
              <p className="font-mono text-[10px] uppercase tracking-wider text-mute">
                Issued {kickoff(data.weather.issued_at)}
              </p>
              {(data.weather.alerts ?? []).map((alert) => (
                <p key={alert.event + (alert.headline || "")} className="text-sm text-heat">
                  {alert.event}
                  {alert.headline ? ` · ${alert.headline}` : ""}
                </p>
              ))}
            </div>
          ) : (
            <p className="text-sm leading-relaxed text-mute">
              No forecast on file. Missing weather is not fair weather.
            </p>
          )}
        </Panel>

        <Panel title="Player availability" hint="Healthy is never assumed">
          {data.injuries.length === 0 ? (
            <div className="space-y-3">
              <p className="font-display text-3xl uppercase leading-none text-heat">Incomplete</p>
              <p className="text-sm leading-relaxed text-paper/80">
                No sourced injury rows for this game. Confidence stays cut — absence of a report is not a
                clean bill of health.
              </p>
            </div>
          ) : (
            <ul className="space-y-3">
              {data.injuries.map((i) => (
                <li key={i.player_id + i.reported_at} className="border-b border-white/5 pb-3 last:border-0 last:pb-0">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-medium">{i.player_name}</span>
                    <span className="font-mono text-[11px] uppercase tracking-wider text-heat">
                      {i.designation.replaceAll("_", " ")}
                    </span>
                  </div>
                  <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                    {Math.round(i.confidence * 100)}% confidence · {i.source.replaceAll("_", " ")}
                    {!i.coverage_complete ? " · coverage incomplete" : ""}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <section className="space-y-3">
        <div>
          <p className="kicker">Edges to inspect</p>
          <h2 className="mt-2 font-display text-4xl uppercase leading-none tracking-wide">Model vs market</h2>
        </div>
        <div className="plate p-5">
          <ModelGaps gaps={data.model_disagreement} props={data.props} />
        </div>
      </section>

      <ResearchPanel research={data.research} />

      <section className="space-y-3">
        <div>
          <p className="kicker">Inputs</p>
          <h2 className="mt-2 font-display text-4xl uppercase leading-none tracking-wide">
            What moved the number
          </h2>
        </div>
        <div className="plate p-5">
          <UsagePanel features={data.key_features} props={data.props} />
        </div>
      </section>
      </details>

      <section className="space-y-3">
        <div>
          <p className="kicker">Priced outcomes</p>
          <h2 className="mt-2 font-display text-4xl uppercase leading-none tracking-wide">Player markets</h2>
          <p className="mt-2 max-w-2xl text-sm text-mute">
            Filter by market type. Probability is the official model number; EV needs a book price.
          </p>
        </div>
        <GameMarkets rows={data.props} />
      </section>
    </div>
  );
}

function weatherLine(weather: GameDetail["weather"]) {
  if (!weather) return "No forecast on file.";
  const summary = weather.summary ? ` ${weather.summary}.` : "";
  return `${cToF(weather.temperature_c)}, wind ${mpsToMph(weather.wind_mps)}.${summary}`;
}

function injuryLine(injuries: GameDetail["injuries"]) {
  if (injuries.length === 0) return "No injury report on file.";
  const notable = injuries.filter((row) => /out|doubt|question/i.test(row.designation));
  const shown = (notable.length ? notable : injuries).slice(0, 3);
  const names = shown.map((row) => `${row.player_name} ${row.designation.replaceAll("_", " ")}`).join(", ");
  const more = injuries.length > shown.length ? ` +${injuries.length - shown.length} more` : "";
  return `Availability: ${names}${more}.`;
}

function firstSentence(text: string): string {
  const trimmed = text.trim();
  const match = trimmed.match(/^(.+?[.!?])(?:\s|$)/);
  return match ? match[1] : trimmed;
}

function Fact({ children }: { children: React.ReactNode }) {
  return (
    <span className="border border-white/10 bg-ink-950 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
      {children}
    </span>
  );
}

function Panel({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="plate p-5">
      <div className="mb-4">
        <h2 className="kicker">{title}</h2>
        {hint ? <p className="mt-1 text-xs text-mute">{hint}</p> : null}
      </div>
      {children}
    </div>
  );
}

type CardRow = {
  game_id: string;
  pick_label?: string | null;
  pick_chance_label?: string | null;
  pick_probability?: number | null;
  p_cover?: number | null;
  pick_market?: string | null;
  pick_result?: "correct" | "miss" | "push" | null;
  ats_pick_label?: string | null;
  ats_side?: "home" | "away" | null;
  ats_team?: string | null;
  ats_line?: number | null;
  ats_cover_margin?: number | null;
  ats_pick_result?: "correct" | "miss" | "push" | null;
  game_status?: string | null;
  home_score?: number | null;
  away_score?: number | null;
  home_team: string;
  away_team: string;
  favored_side?: string;
  home_handicap?: number | null;
  pick_team?: string | null;
  predicted_margin?: number | null;
  predicted_home_score?: number | null;
  predicted_away_score?: number | null;
  projection_withheld?: boolean;
};

async function loadCard(leagueId: string, gameId: string): Promise<CardRow | null> {
  try {
    const board = await apiGet<{ rows: CardRow[] }>(`/v1/ats?league=${leagueId}`, 20000);
    return board.rows.find((row) => row.game_id === gameId) ?? null;
  } catch {
    return null;
  }
}

function CardReadout({
  row,
  awayColor,
  homeColor,
}: {
  row: CardRow;
  awayColor: string;
  homeColor: string;
}) {
  const settled = row.game_status === "final";
  return (
    <div className="plate relative mt-6 overflow-hidden px-4 py-4 sm:px-5">
      <span aria-hidden className="absolute bottom-0 left-0 top-0 w-1.5" style={{ background: awayColor }} />
      <span aria-hidden className="absolute bottom-0 right-0 top-0 w-1.5" style={{ background: homeColor }} />
      <PickPair row={row} />
      {settled && row.away_score != null && row.home_score != null ? (
        <p className="mt-3 font-mono text-[11px] uppercase tracking-wider text-mute">
          Final {row.away_score}–{row.home_score}
        </p>
      ) : (
        <MarginBar
          margin={row.predicted_margin}
          handicap={row.home_handicap}
          awayColor={awayColor}
          homeColor={homeColor}
          awayName={row.away_team}
          homeName={row.home_team}
        />
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-white/5 bg-black/20 px-3 py-2">
      <dt className="font-mono text-[10px] uppercase tracking-wider text-mute">{label}</dt>
      <dd className="mt-1 text-sm text-paper">{value}</dd>
    </div>
  );
}
