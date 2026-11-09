import type { ResearchDetail } from "@/lib/api";
import {
  claimLabel,
  recommendationCopy,
  scenarioLabel,
  topicLabel,
  validationLabel,
} from "@/lib/labels";

export function ResearchPanel({ research }: { research: ResearchDetail | null | undefined }) {
  if (!research) {
    return (
      <section className="plate p-5">
        <h2 className="kicker mb-3">Research brief</h2>
        <p className="text-sm leading-relaxed text-mute">
          No cached read yet. The worker writes one before kickoff. Research never changes the official
          probability.
        </p>
      </section>
    );
  }

  const explanation = research.explanations[0];
  const call = recommendationCopy(explanation?.recommendation_status);
  const opportunities = research.opportunities ?? [];

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="kicker">Research brief</p>
          <h2 className="mt-2 font-display text-4xl uppercase leading-none tracking-wide">What the desk sees</h2>
        </div>
        <p className="max-w-sm text-right font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
          {research.model} · {research.prompt_version}
          <br />
          Official number unchanged · play off
        </p>
      </div>

      {explanation ? (
        <div className="plate p-5">
          <h3 className="kicker mb-1">Why the call stands</h3>
          <p className="mb-4 text-xs text-mute">
            Full read behind the {call.title.toLowerCase()} above. Official probabilities are unchanged.
          </p>
          <p className="max-w-3xl text-sm leading-relaxed text-paper/85">{explanation.narrative}</p>
          <div className="mt-5 grid gap-4 md:grid-cols-3">
            <FactList title="What supports it" items={explanation.factors} />
            <FactList title="Assumptions" items={explanation.assumptions} />
            <FactList title="Uncertainty" items={explanation.uncertainty} />
          </div>
        </div>
      ) : research.headline ? (
        <p className="max-w-3xl text-sm leading-relaxed text-paper">{research.headline}</p>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="plate p-5">
          <h3 className="kicker mb-1">Sourced findings</h3>
          <p className="mb-4 text-xs text-mute">Only claims with evidence on file.</p>
          {research.findings.length === 0 ? (
            <p className="text-sm text-mute">Nothing sourced in this packet.</p>
          ) : (
            <ul className="space-y-4">
              {research.findings.map((finding) => (
                <li key={finding.statement} className="border-l-2 border-gold/40 pl-3">
                  <div className="text-sm leading-relaxed text-paper">{finding.statement}</div>
                  <div className="mt-1.5 flex flex-wrap gap-2 font-mono text-[10px] uppercase tracking-wider text-mute">
                    <span>{claimLabel(finding.claim_type)}</span>
                    <span>{topicLabel(finding.topic)}</span>
                    <span>{Math.round(finding.confidence * 100)}% confidence</span>
                    {finding.evidence.length ? (
                      <span>{finding.evidence.map((row) => row.source.replaceAll("_", " ")).join(", ")}</span>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="plate p-5">
          <h3 className="kicker mb-1">Market chatter</h3>
          <p className="mb-4 text-xs text-mute">
            Speculative notes from prices. Not a model input and not a recommendation.
          </p>
          {research.signals.length === 0 ? (
            <p className="text-sm text-mute">No material workload change is on file.</p>
          ) : (
            <ul className="space-y-4">
              {research.signals.map((signal) => (
                <li key={`${signal.player_id}-${signal.metric}-${signal.statement}`}>
                  <div className="text-sm leading-relaxed text-paper">{signal.statement}</div>
                  <div className="mt-1.5 flex flex-wrap gap-2 font-mono text-[10px] uppercase tracking-wider text-mute">
                    <span>{(signal.metric || "signal").replaceAll("_", " ")}</span>
                    <span>{validationLabel(signal.validation_status)}</span>
                    <span>{scenarioLabel(signal.scenario_status)}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="plate p-5">
        <h3 className="kicker mb-1">Queued opportunities</h3>
        <p className="mb-4 text-xs text-mute">
          Evidence-backed items waiting for statistical review. The model never invents an edge here.
        </p>
        {opportunities.length === 0 ? (
          <p className="text-sm text-mute">Nothing queued for review on this game.</p>
        ) : (
          <ul className="space-y-4">
            {opportunities.map((row) => (
              <li key={`${row.player_id}-${row.market_id}-${row.explanation.slice(0, 24)}`}>
                <div className="text-sm leading-relaxed font-medium text-paper">{row.explanation}</div>
                <div className="mt-1.5 flex flex-wrap gap-2 font-mono text-[10px] uppercase tracking-wider text-mute">
                  <span>{row.market_id.replaceAll("_", " ")}</span>
                  <span>{claimLabel(row.claim_type)}</span>
                  <span>Evidence {Math.round(row.evidence_confidence * 100)}%</span>
                  <span>{validationLabel(row.validation_status)}</span>
                </div>
                {row.missing_information?.length ? (
                  <p className="mt-1 text-xs text-mute">Still missing: {row.missing_information.join("; ")}</p>
                ) : null}
                {row.recommended_analysis ? (
                  <p className="mt-1 text-xs text-paper/80">Next step: {row.recommended_analysis}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      {research.alerts.length > 0 ? (
        <div className="plate p-5">
          <h3 className="kicker mb-3">What changed since last read</h3>
          <ul className="space-y-2 text-sm text-paper/90">
            {research.alerts.map((alert) => (
              <li key={alert.kind + alert.summary}>{alert.summary}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {(research.prediction_reviews?.length ?? 0) > 0 ? (
        <div className="plate p-5">
          <h3 className="kicker mb-1">Prediction review flags</h3>
          <p className="mb-3 text-xs text-mute">
            Test suggestions only. Applied stays false. Probabilities are not overridden.
          </p>
          <ul className="space-y-3 text-sm">
            {research.prediction_reviews!.map((row) => (
              <li key={`${row.forecast_id}-${row.origin}`}>
                <div className="font-medium">{row.origin.replaceAll("_", " ")}</div>
                <div className="text-xs text-paper/80">{row.suggested_experiment}</div>
                <div className="font-mono text-[10px] uppercase tracking-wider text-mute">
                  Applied {row.applied ? "yes" : "no"}
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function FactList({ title, items }: { title: string; items: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <h4 className="font-mono text-[10px] uppercase tracking-[0.18em] text-gold">{title}</h4>
      <ul className="mt-2 space-y-2 text-xs leading-relaxed text-paper/75">
        {items.map((item) => (
          <li key={item}>• {item}</li>
        ))}
      </ul>
    </div>
  );
}
