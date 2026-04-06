# Phase 8: User Model, Self Model & Privacy Filter

## Motivation

These three concerns share a common boundary: the memory system's trust interface. The user model
stores personal data (personality, values, energy patterns) and needs privacy awareness at the
storage layer. The self model tracks prediction accuracy per domain and feeds autonomy decisions.
The privacy filter gates what enters the event log at all.

Together they implement the trust boundary: what Theo knows about the user (user model), how much
Theo trusts its own judgment (self model), and what Theo is allowed to store (privacy filter). The
privacy filter must be in place before memory tools go live, because events are immutable forever --
rejection at the boundary is the only defense.

## Depends on

- **Phase 3** -- Event bus (mutations emit events)
- **Phase 4** -- Memory schema (tables exist)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/memory/user_model.ts` | `UserModelRepository` -- dimension CRUD, per-dimension confidence thresholds |
| `src/memory/self_model.ts` | `SelfModelRepository` -- prediction/outcome tracking, calibration |
| `src/memory/privacy.ts` | `detectSensitivity()`, `checkPrivacy()` -- pure functions, no state, no DB |
| `tests/memory/user_model.test.ts` | Dimension CRUD, confidence computation, threshold behavior |
| `tests/memory/self_model.test.ts` | Prediction recording, outcome tracking, calibration |
| `tests/memory/privacy.test.ts` | Sensitivity detection, trust tier enforcement, regex coverage |

## Design Decisions

### User Model

#### Evidence Semantics

Each call to `updateDimension()` represents one or more observed behavioral signals. The `evidence`
parameter defaults to 1. A single conversation that surfaces three distinct observations supporting
a dimension (e.g., three separate moments where the user demonstrates a preference for direct
communication) would pass `evidence=3`. Evidence is cumulative -- it only increases. The count
reflects the total number of independent observations across all time, not a sliding window.

#### Per-Dimension Confidence Thresholds

Different dimensions require different amounts of evidence before Theo should trust its assessment.
A communication style preference might be clear after a few interactions, while deep personality
patterns need sustained observation. Each dimension category has a threshold -- the number of
evidence signals required to reach full confidence.

```typescript
// Default thresholds per dimension category.
// Confidence = min(1.0, evidence_count / threshold).
// Higher threshold = more evidence needed before Theo trusts its read.
const CONFIDENCE_THRESHOLDS: Record<string, number> = {
  personality_type: 20,          // needs sustained observation
  communication_style: 5,        // observable quickly from conversation patterns
  values: 15,                    // emerges over time through decisions and reactions
  energy_patterns: 10,           // moderate evidence from scheduling behavior
  boundaries: 3,                 // few clear signals are definitive
  cognitive_preferences: 8,      // shows up in how user frames problems
  shadow_patterns: 25,           // very high evidence needed -- easy to misread
  archetypes: 20,                // deep pattern, needs many observations
  individuation_markers: 30,     // highest evidence needed -- growth is slow
  _default: 10,                  // fallback for unknown dimensions
};

function getThreshold(dimensionName: string): number {
  return CONFIDENCE_THRESHOLDS[dimensionName] ?? CONFIDENCE_THRESHOLDS["_default"]!;
}
```

#### Types and Repository

```typescript
interface UserModelDimension {
  readonly id: number;
  readonly name: string;
  readonly value: unknown;        // JsonValue -- structure varies per dimension
  readonly confidence: number;    // 0.0 to 1.0
  readonly evidenceCount: number;
  readonly threshold: number;     // looked up from CONFIDENCE_THRESHOLDS
  readonly createdAt: Date;
  readonly updatedAt: Date;
}

class UserModelRepository {
  async getDimensions(): Promise<readonly UserModelDimension[]> { ... }

  async getDimension(name: string): Promise<UserModelDimension | null> { ... }

  async updateDimension(
    name: string,
    value: unknown,
    evidence: number = 1,
    actor: Actor,
  ): Promise<UserModelDimension> {
    // Look up the threshold for this dimension category.
    // The threshold lives in application code, not in the database --
    // changing it retroactively recomputes confidence for all future reads.
    const threshold = getThreshold(name);

    const [row] = await this.sql`
      INSERT INTO user_model_dimension (name, value, evidence_count, confidence)
      VALUES (
        ${name},
        ${this.sql.json(value)},
        ${evidence},
        LEAST(1.0, ${evidence}::real / ${threshold})
      )
      ON CONFLICT (name) DO UPDATE SET
        value = ${this.sql.json(value)},
        evidence_count = user_model_dimension.evidence_count + ${evidence},
        confidence = LEAST(1.0, (user_model_dimension.evidence_count + ${evidence})::real / ${threshold})
    RETURNING *`;

    await this.bus.emit({
      type: "memory.user_model.updated",
      version: 1,
      actor,
      data: { dimension: name, confidence: row.confidence },
      metadata: {},
    });

    return rowToDimension(row, threshold);
  }
}
```

#### Dimensions

Seeded during onboarding, refined over time:

- `personality_type` -- MBTI/Big Five indicators (threshold: 20)
- `values` -- Schwartz basic values (threshold: 15)
- `communication_style` -- direct/nuanced, detail/big-picture (threshold: 5)
- `energy_patterns` -- daily rhythms, peak hours (threshold: 10)
- `boundaries` -- topics off-limits, autonomy limits (threshold: 3)
- `cognitive_preferences` -- learning style, reasoning approach (threshold: 8)
- `shadow_patterns` -- Jungian shadow observations (threshold: 25)
- `archetypes` -- dominant archetypal patterns (threshold: 20)
- `individuation_markers` -- growth trajectory observations (threshold: 30)

### Self Model

```typescript
interface SelfModelDomain {
  readonly id: number;
  readonly name: string;
  readonly predictions: number;
  readonly correct: number;
  readonly createdAt: Date;
  readonly updatedAt: Date;
}
```

The flow has two steps:

1. **`recordPrediction()`** -- called when Theo makes a prediction. Increments `predictions` by 1.
   This is the commitment: "I think X will happen."
2. **`recordOutcome()`** -- called when the outcome is known. If `correct=true`, increments
   `correct` by 1. If `correct=false`, does nothing to the counters (predictions was already
   incremented). Either way, emits a `memory.self_model.updated` event for the audit trail.

The separation matters: `recordOutcome()` always emits the event regardless of correctness, so the
event log captures both hits and misses. The calibration ratio is `correct / predictions`.

```typescript
class SelfModelRepository {
  async recordPrediction(domain: string, actor: Actor): Promise<void> {
    // Upsert: create domain if new, increment predictions counter.
    await this.sql`
      INSERT INTO self_model_domain (name, predictions) VALUES (${domain}, 1)
      ON CONFLICT (name) DO UPDATE SET predictions = self_model_domain.predictions + 1
    `;

    await this.bus.emit({
      type: "memory.self_model.updated",
      version: 1,
      actor,
      data: {
        domain,
        calibration: await this.getCalibration(domain),
      },
      metadata: {},
    });
  }

  async recordOutcome(domain: string, correct: boolean, actor: Actor): Promise<void> {
    // Only increment `correct` when the prediction was right.
    // `predictions` was already incremented by recordPrediction().
    // An incorrect outcome needs no counter update -- the miss is captured
    // by the gap between predictions and correct.
    if (correct) {
      await this.sql`
        UPDATE self_model_domain SET correct = correct + 1 WHERE name = ${domain}
      `;
    }

    // Always emit, regardless of correctness.
    // The event log captures every outcome for the audit trail.
    await this.bus.emit({
      type: "memory.self_model.updated",
      version: 1,
      actor,
      data: {
        domain,
        correct,
        calibration: await this.getCalibration(domain),
      },
      metadata: {},
    });
  }

  async getCalibration(domain: string): Promise<number> {
    const [row] = await this.sql`
      SELECT predictions, correct FROM self_model_domain WHERE name = ${domain}
    `;
    if (!row || row.predictions === 0) return 0;
    return row.correct / row.predictions;
  }
}
```

Domains: `scheduling`, `drafting`, `recommendations`, `memory_relevance`, `goal_planning`,
`mood_assessment`, `session_management`.

The `session_management` domain tracks Theo's accuracy in deciding when to start new sessions vs.
continue existing ones. The smart session manager (Phase 10) records predictions here, and user
corrections (e.g., "we were still talking about that") record outcomes. This enables the session
heuristic to calibrate over time.

### Privacy Filter

The privacy filter is a set of **pure functions** -- no database, no state, no side effects. It
classifies content sensitivity using regex heuristics and enforces trust tier limits. Pure functions
make it trivially testable and impossible to accidentally couple to runtime state.

#### Sensitivity Ordering

The ordering reflects **exploitability by a malicious third party**, not subjective emotional
sensitivity:

```typescript
type Sensitivity = "normal" | "location" | "relationship" | "identity" | "medical" | "financial";

// Numeric levels for comparison. Higher = more exploitable.
const SENSITIVITY_LEVEL: Record<Sensitivity, number> = {
  normal: 0,       // no sensitivity
  location: 1,     // physical safety implications (stalking, burglary)
  relationship: 2, // interpersonal privacy (social engineering vector)
  identity: 3,     // identity theft risk (passport, driver's license)
  medical: 4,      // health privacy, heavily regulated (HIPAA)
  financial: 5,    // direct fraud risk -- most actionable by attackers (SSN, credit cards)
};
```

Rationale for the ordering:

- **Financial (5):** SSNs and credit card numbers enable direct, immediate fraud. An attacker with a
  credit card number can make purchases within minutes. This is the most exploitable category.
- **Medical (4):** Health data is heavily regulated (HIPAA in the US, GDPR special category in the
  EU). Exposure creates legal liability and can be used for insurance fraud or discrimination.
- **Identity (3):** Passport and license numbers enable identity theft, but exploiting them
  typically requires additional steps (forging documents, opening accounts).
- **Relationship (2):** Personal relationship details are social engineering vectors but require
  interpretation and context to exploit.
- **Location (1):** Physical addresses and GPS coordinates have safety implications (stalking,
  burglary) but are often semi-public information.
- **Normal (0):** No sensitivity concern.

#### Sensitivity Detection (Regex Heuristics)

```typescript
interface SensitivityMatch {
  readonly category: Sensitivity;
  readonly label: string;
}

const SENSITIVITY_PATTERNS: ReadonlyArray<{
  readonly category: Sensitivity;
  readonly pattern: RegExp;
  readonly label: string;
}> = [
  // Financial -- highest exploitability
  { category: "financial", pattern: /\b\d{3}-\d{2}-\d{4}\b/, label: "SSN" },
  {
    category: "financial",
    pattern: /\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b/,
    label: "credit card",
  },
  {
    category: "financial",
    pattern: /\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b/,
    label: "IBAN",
  },

  // Medical
  { category: "medical", pattern: /\bdiagnos(?:ed|is)\b.*?\b(?:with|of)\b/i, label: "diagnosis" },
  {
    category: "medical",
    pattern: /\b(?:prescribed?|prescription|dosage|mg\/day)\b/i,
    label: "prescription",
  },
  { category: "medical", pattern: /\b[A-Z]\d{2}(?:\.\d{1,4})?\b/, label: "ICD code" },

  // Identity
  {
    category: "identity",
    pattern: /\bpassport\s*(?:#|no|number)?\s*[:.]?\s*[A-Z0-9]{6,9}\b/i,
    label: "passport",
  },
  {
    category: "identity",
    pattern: /\b(?:driver'?s?\s*licen[cs]e|DL)\s*(?:#|no|number)?\s*[:.]?\s*[A-Z0-9]{5,15}\b/i,
    label: "drivers license",
  },

  // Location
  {
    category: "location",
    pattern: /\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:St|Ave|Blvd|Rd|Dr|Ln|Way|Ct)\b/,
    label: "street address",
  },
  {
    category: "location",
    pattern: /-?\d{1,3}\.\d{4,},\s*-?\d{1,3}\.\d{4,}/,
    label: "GPS coordinates",
  },
];

/**
 * Scan content for sensitive data patterns.
 * Returns the highest-severity match, or { category: "normal" } if nothing detected.
 * Pure function -- no state, no side effects.
 */
function detectSensitivity(content: string): SensitivityMatch {
  let highest: SensitivityMatch = { category: "normal", label: "none" };
  let highestLevel = 0;

  for (const entry of SENSITIVITY_PATTERNS) {
    if (entry.pattern.test(content)) {
      const level = SENSITIVITY_LEVEL[entry.category];
      if (level > highestLevel) {
        highest = { category: entry.category, label: entry.label };
        highestLevel = level;
      }
    }
  }

  return highest;
}
```

#### Trust Tier Enforcement

```typescript
type PrivacyDecision =
  | { readonly allowed: true }
  | { readonly allowed: false; readonly reason: string; readonly category: Sensitivity };

const TRUST_SENSITIVITY_MAP: Record<TrustTier, Sensitivity> = {
  owner: "financial",            // can store anything
  owner_confirmed: "financial",  // can store anything
  verified: "location",          // up to location data
  inferred: "normal",            // non-sensitive only
  external: "normal",            // non-sensitive only
  untrusted: "normal",           // non-sensitive only
};

/**
 * Gate function: should this content be allowed into the event log?
 * Pure function -- no database, no state, no side effects.
 *
 * Compares detected sensitivity against the maximum allowed for the trust tier.
 * Returns { allowed: true } or { allowed: false, reason, category }.
 */
function checkPrivacy(content: string, trustTier: TrustTier): PrivacyDecision {
  const detected = detectSensitivity(content);
  const maxAllowed = TRUST_SENSITIVITY_MAP[trustTier];

  if (SENSITIVITY_LEVEL[detected.category] > SENSITIVITY_LEVEL[maxAllowed]) {
    return {
      allowed: false,
      reason: `Content contains ${detected.label} ` +
        `(${detected.category}), which exceeds the ` +
        `${trustTier} trust tier limit (max: ${maxAllowed})`,
      category: detected.category,
    };
  }

  return { allowed: true };
}
```

## Definition of Done

- [ ] `UserModelRepository.updateDimension()` upserts dimension, increments evidence, computes
  confidence using per-dimension threshold
- [ ] `UserModelRepository.getDimensions()` returns all dimensions with confidence scores
- [ ] `getThreshold()` returns correct threshold per dimension name with `_default` fallback
- [ ] `SelfModelRepository.recordPrediction()` increments prediction count, emits event
- [ ] `SelfModelRepository.recordOutcome()` increments `correct` only when `correct=true`, always
  emits event
- [ ] `SelfModelRepository.getCalibration()` returns `correct / predictions` ratio
- [ ] `detectSensitivity()` is a pure function -- no state, no DB
- [ ] `checkPrivacy()` is a pure function -- no state, no DB
- [ ] `detectSensitivity()` matches SSN patterns and returns `{ category: "financial", label: "SSN"
  }`
- [ ] `detectSensitivity()` matches credit card patterns
- [ ] `detectSensitivity()` returns highest-severity match when multiple patterns match
- [ ] `checkPrivacy()` blocks financial data for non-owner trust tiers
- [ ] `checkPrivacy()` allows everything for `owner` trust tier
- [ ] `checkPrivacy()` allows normal text for all trust tiers
- [ ] `just check` passes

## Test Cases

### `tests/memory/user_model.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Create dimension | New dimension `communication_style`, evidence=1 | Row created, confidence = 1/5 = 0.2 |
| Update dimension | Existing `communication_style`, add evidence=2 | evidence_count=3, confidence = 3/5 = 0.6 |
| Confidence caps at 1.0 | `boundaries` (threshold=3), evidence=10 | confidence = 1.0, not > 1.0 |
| High-threshold dimension | `individuation_markers`, evidence=1 | confidence = 1/30 = 0.033 |
| Default threshold | Unknown dimension `custom_dim`, evidence=5 | Uses _default threshold=10, confidence = 0.5 |
| Get all dimensions | Multiple dimensions created | Returns all with correct confidence values |
| Get single dimension | Specific name | Returns matching or null |
| Multiple evidence per call | `energy_patterns`, evidence=3 (three observations in one conversation) | evidence_count=3, confidence = 3/10 = 0.3 |
| Event emitted | Update any dimension | `memory.user_model.updated` event with dimension name and confidence |

### `tests/memory/self_model.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Record prediction | New domain `scheduling` | predictions=1, correct=0 |
| Record correct outcome | Existing domain, correct=true | correct incremented, event emitted |
| Record incorrect outcome | Existing domain, correct=false | correct NOT incremented, event still emitted |
| Calibration 100% | 5 predictions, 5 correct | calibration = 1.0 |
| Calibration 50% | 4 predictions, 2 correct | calibration = 0.5 |
| Calibration 0% | 3 predictions, 0 correct | calibration = 0.0 |
| No predictions | Missing domain | calibration = 0 |
| Event emitted on prediction | recordPrediction() called | `memory.self_model.updated` event |
| Event emitted on outcome (correct) | recordOutcome(domain, true) | event includes `correct: true` |
| Event emitted on outcome (incorrect) | recordOutcome(domain, false) | event includes `correct: false` |

### `tests/memory/privacy.test.ts`

This deserves the most thorough test suite in the project -- the privacy filter is the last line of
defense before immutable storage.

#### `detectSensitivity()` (pure function tests)

| Test | Input | Expected |
| ------ | ------- | ---------- |
| Clean text | "User likes coffee" | `{ category: "normal", label: "none" }` |
| SSN pattern | "SSN: 123-45-6789" | `{ category: "financial", label: "SSN" }` |
| Credit card (Visa) | "card 4111111111111111" | `{ category: "financial", label: "credit card" }` |
| Credit card (Mastercard) | "pay with 5500000000000004" | `{ category: "financial", label: "credit card" }` |
| Credit card (Amex) | "amex 340000000000009" | `{ category: "financial", label: "credit card" }` |
| IBAN | "transfer to GB29NWBK60161331926819" | `{ category: "financial", label: "IBAN" }` |
| Diagnosis | "diagnosed with diabetes" | `{ category: "medical", label: "diagnosis" }` |
| Prescription | "prescribed 50mg/day" | `{ category: "medical", label: "prescription" }` |
| ICD code | "code E11.65 in chart" | `{ category: "medical", label: "ICD code" }` |
| Passport number | "passport #AB1234567" | `{ category: "identity", label: "passport" }` |
| Drivers license | "driver's license DL12345678" | `{ category: "identity", label: "drivers license" }` |
| Street address | "lives at 123 Main St" | `{ category: "location", label: "street address" }` |
| GPS coordinates | "37.7749, -122.4194" | `{ category: "location", label: "GPS coordinates" }` |
| Partial SSN | "123-45" (not full pattern) | `{ category: "normal", label: "none" }` |
| Empty text | "" | `{ category: "normal", label: "none" }` |
| Numbers in context | "Room 123, Floor 4" | `{ category: "normal", label: "none" }` |
| Multiple patterns (SSN + address) | "SSN 123-45-6789 at 123 Main St" | `{ category: "financial", label: "SSN" }` (highest severity wins) |

#### `checkPrivacy()` (trust tier enforcement)

| Test | Content | Trust Tier | Expected |
| ------ | --------- | ------------ | ---------- |
| Normal text, any tier | "User likes coffee" | inferred | allowed |
| SSN, owner | "SSN: 123-45-6789" | owner | allowed |
| SSN, owner_confirmed | "SSN: 123-45-6789" | owner_confirmed | allowed |
| SSN, verified | "SSN: 123-45-6789" | verified | blocked (financial > location) |
| SSN, inferred | "SSN: 123-45-6789" | inferred | blocked (financial > normal) |
| SSN, external | "SSN: 123-45-6789" | external | blocked (financial > normal) |
| GPS, verified | "37.7749, -122.4194" | verified | allowed (location <= location) |
| GPS, inferred | "37.7749, -122.4194" | inferred | blocked (location > normal) |
| Medical, owner | "diagnosed with diabetes" | owner | allowed |
| Medical, verified | "diagnosed with diabetes" | verified | blocked (medical > location) |
| Empty text, untrusted | "" | untrusted | allowed |

## Risks

**Medium risk.** The privacy filter regex heuristics will never be perfect -- there will always be
false positives (blocking innocent text that looks like a pattern) and false negatives (missing
creative formatting of sensitive data). The mitigations:

1. Be strict on high-exploitability patterns (SSN, credit cards, IBAN) where false positives are
   acceptable
2. Accept that contextual categories (relationship details, medical mentions in casual conversation)
   will have edge cases
3. Keep the pattern list extensible -- `SENSITIVITY_PATTERNS` is a flat array, adding a new pattern
   is one line
4. Pure functions make every edge case trivially testable -- no mocking, no setup

The ICD code pattern (`/\b[A-Z]\d{2}(?:\.\d{1,4})?\b/`) will produce false positives on strings like
"A12" or "B99". This is an acceptable tradeoff -- better to over-flag than to leak medical codes. If
false positives become a problem in practice, the pattern can be tightened to require the dot-suffix
(`[A-Z]\d{2}\.\d{1,4}`).

The user model's JSONB values are schema-flexible by design. The application layer must validate
structure when reading dimension values. The confidence thresholds live in application code (not in
the database) so they can be tuned without migrations.
