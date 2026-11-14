import { cookies } from "next/headers";

const API = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function sessionToken(): Promise<string> {
  const jar = await cookies();
  return jar.get("caliber_session")?.value ?? "";
}

/** Reuse saved boards in the Netlify SSR process across navigations. */
const GET_CACHE_TTL_MS = 5 * 60_000;
const getCache = new Map<string, { expires: number; value: unknown }>();

function cacheableGetPath(path: string): boolean {
  const pathname = path.split("?")[0];
  return pathname === "/v1/days" || pathname === "/v1/anytime-td/research";
}

export async function apiGet<T>(path: string, timeoutMs = 15000): Promise<T> {
  const session = await sessionToken();
  const cacheKey = path;
  const useCache = !session && cacheableGetPath(path);
  if (useCache) {
    const hit = getCache.get(cacheKey);
    if (hit && hit.expires > Date.now()) {
      return hit.value as T;
    }
  }
  // AbortSignal.timeout is missing on some Netlify runtimes and throws before fetch.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, {
      cache: "no-store",
      signal: ctrl.signal,
      headers: session ? { Authorization: `Bearer ${session}` } : undefined,
    });
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    throw new Error(`${path} failed: ${res.status}`);
  }
  const value = (await res.json()) as T;
  if (useCache) {
    getCache.set(cacheKey, { expires: Date.now() + GET_CACHE_TTL_MS, value });
  }
  return value;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${path} failed: ${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
}

export type PriceFreshness = {
  status: "fresh" | "stale" | "missing" | string;
  age_minutes: number | null;
  captured_at: string | null;
  source_timestamp: string | null;
  max_age_minutes: number;
  is_current_opportunity: boolean;
};

export type Forecast = {
  prediction_as_of: string;
  model_version: string;
  data_cutoff: string;
  markets: string[];
  p_estimate: number;
  confidence_interval: [number, number];
  book_price: number | null;
  source_timestamp: string | null;
  price_freshness?: PriceFreshness;
  availability_status: string;
  reason_codes: string[];
  matchup?: { kind: "rush" | "pass"; factor: number } | null;
  tonight?: {
    depth_rank: number | null;
    carries_per_game: number;
    targets_per_game: number;
    recent_tds: number;
    games: number;
    summary: string;
  } | null;
  disclaimer: string;
  ev: number | null;
  ev_after_haircut: number | null;
  recommendation_status: string | null;
  data_tier?: string;
  research_only?: boolean;
  validated_play?: boolean;
  play_enabled?: boolean;
  side?: string;
  line?: number | null;
};

export type SlateGame = {
  game_id: string;
  league_id: "nfl" | "fbs" | string;
  week: number | null;
  kickoff_utc: string;
  status: string;
  home: {
    team_id: string;
    name: string;
    abbreviation: string;
    conference: string | null;
    mark?: { location?: string; nick?: string; primary?: string; secondary?: string; logo?: string } | null;
  };
  away: {
    team_id: string;
    name: string;
    abbreviation: string;
    conference: string | null;
    mark?: { location?: string; nick?: string; primary?: string; secondary?: string; logo?: string } | null;
  };
  venue: { name: string; roof_type: string; surface: string };
  weather: {
    temperature_c: number | null;
    wind_mps: number | null;
    gust_mps: number | null;
    precip_mm: number | null;
    precip_probability?: number | null;
    summary?: string | null;
    alerts?: Array<{ event: string; headline?: string; severity?: string | null }>;
    roof_status: string;
    issued_at: string;
    provider?: string;
    quality_status?: string;
  } | null;
  model_freshness_minutes: number | null;
  conference: string | null;
  data_tier?: string;
  research?: ResearchSummary | null;
};

export type ResearchSummary = {
  status: string;
  as_of: string;
  data_tier: string;
  headline: string | null;
  finding_count: number;
  signal_count: number;
  opportunity_count?: number;
  official_unchanged: boolean;
  play_enabled?: boolean;
};

export type ResearchEvidence = {
  source: string;
  ref: string;
  observed_at?: string | null;
};

export type ResearchSignal = {
  player_id: string;
  metric?: string;
  market_id?: string;
  direction?: string;
  magnitude?: string;
  claim_type: string;
  statement?: string;
  explanation?: string;
  confidence?: number;
  evidence_confidence?: number;
  evidence?: ResearchEvidence[];
  missing_information?: string[];
  recommended_analysis?: string;
  validation_status: string;
  scenario_status: string;
  research_only: boolean;
  numeric_edge?: null;
  play_enabled?: boolean;
};

export type ResearchOpportunity = {
  player_id: string;
  game_id: string;
  market_id: string;
  claim_type: string;
  explanation: string;
  evidence_confidence: number;
  evidence: ResearchEvidence[];
  missing_information: string[];
  recommended_analysis: string;
  validation_status: string;
  research_only: boolean;
  numeric_edge: null;
  play_enabled: boolean;
};

export type ResearchDetail = ResearchSummary & {
  model: string;
  prompt_version: string;
  findings: Array<{
    topic: string;
    claim_type: string;
    statement: string;
    confidence: number;
    evidence: ResearchEvidence[];
  }>;
  signals: ResearchSignal[];
  opportunities?: ResearchOpportunity[];
  alerts: Array<{ kind: string; summary: string; model_response: string }>;
  explanations: Array<{
    recommendation_status: string;
    reason_codes: string[];
    narrative: string;
    factors: string[];
    assumptions: string[];
    uncertainty: string[];
  }>;
  prediction_reviews?: Array<{
    forecast_id: string;
    origin: string;
    suggested_experiment: string;
    applied: boolean;
  }>;
};

export type Qualification = { eligible: boolean; reasons: string[]; verified_at: string | null; valid_until: string | null; minimum_american_odds: number | null };
export type PropRow = {
  qualification?: Qualification;
  published_pick_id?: string | null;
  locked?: boolean;
  outcome_id: string;
  game_id: string;
  league_id: string;
  market_id: string;
  player_id: string | null;
  player_name: string;
  position?: string | null;
  team: string;
  team_mark?: { location?: string; nick?: string; primary?: string; secondary?: string; logo?: string } | null;
  opponent: string;
  away_abbr?: string;
  home_abbr?: string;
  away_mark?: { location?: string; nick?: string; primary?: string; secondary?: string; logo?: string } | null;
  home_mark?: { location?: string; nick?: string; primary?: string; secondary?: string; logo?: string } | null;
  kickoff_utc: string;
  side: string;
  line: number | null;
  pick?: string | null;
  model_lean?: boolean | null;
  projection?: number | null;
  projection_games?: number | null;
  projection_unit?: string | null;
  projection_source?: string | null;
  bookmaker: string | null;
  book_price: number | null;
  source_timestamp: string | null;
  price_freshness?: PriceFreshness;
  best_available?: boolean;
  forecast: Forecast;
  usage?: {
    carries?: number | null;
    targets?: number | null;
    rz_targets?: number | null;
    gl_carries?: number | null;
    games?: number | null;
  } | null;
  data_tier?: string;
  research_signal?: ResearchSignal | null;
};

export type PropPick = {
  pick_id?: string;
  published_at?: string;
  bookmaker?: string;
  qualification?: Qualification;
  locked?: boolean;
  game_id: string;
  league_id: string;
  away_abbr?: string | null;
  home_abbr?: string | null;
  kickoff_utc?: string | null;
  market_id: string;
  player_name: string;
  team?: string | null;
  side?: string | null;
  line?: number | null;
  p: number;
  book_price: number;
  loud: boolean;
  price_ahead: boolean;
  stale?: boolean;
  sentence: string;
  research_only?: boolean;
};

export type PropPickCard = {
  locked?: boolean;
  date: string;
  league: string;
  games_on_slate: number;
  degraded?: boolean;
  picks: PropPick[];
  note: string;
  disclaimer: string;
};

export type SlateResponse = {
  date: string;
  league: string;
  games: SlateGame[];
  model_versions: Record<string, string>;
  disclaimer: string;
};

export type GameDetail = {
  locked?: boolean;
  game: SlateGame;
  injuries: Array<{
    player_id: string;
    player_name: string;
    designation: string;
    practice_status: string | null;
    confidence: number;
    source: string;
    coverage_complete: boolean;
    reported_at: string;
    quality_status: string;
  }>;
  weather: SlateGame["weather"];
  key_features: Array<{
    player_id: string | null;
    as_of: string;
    version: string;
    coverage: Record<string, unknown>;
    missingness: number;
    blob: Record<string, unknown>;
  }>;
  props: PropRow[];
  model_disagreement: Array<{
    outcome_id: string;
    model_p: number;
    market_implied: number;
    delta: number;
  }>;
  research?: ResearchDetail | null;
  disclaimer: string;
};

export type ParlayLeg = {
  outcome_id: string;
  game_id: string;
  league_id: string;
  player_id: string;
  player_name: string;
  position?: string | null;
  team: string;
  opponent: string;
  away_abbr?: string | null;
  home_abbr?: string | null;
  kickoff_utc: string;
  market_id: string;
  side: string;
  line: number | null;
  book_price: number | null;
  p: number;
};

export type ParlayTicket = {
  id: string;
  kind: string;
  title: string;
  summary: string;
  method: string;
  joint_probability: number;
  independent_probability: number;
  price_american: number | null;
  ev: number | null;
  legs: ParlayLeg[];
};

export type ParlayCard = {
  locked?: boolean;
  locked_count?: number;
  date: string;
  league: string;
  built_at: string;
  games_on_slate: number;
  eligible_legs: number;
  games_with_legs: number;
  tickets: ParlayTicket[];
  note: string;
  disclaimer: string;
};
