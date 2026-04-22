/**
 * OTel semantic convention constants used by Theo's span wrappers.
 *
 * Single source of truth: every span attribute key is either a well-known
 * OTel convention (`db.*`, `messaging.*`, `code.*`, `service.*`, `host.*`)
 * or a Theo-specific key under the `theo.*` namespace.
 *
 * The dashboards test (`dashboards.test.ts`) and the semconv test
 * (`semconv.test.ts`) read this file as the canonical set.
 */

// OTel standard — kept as string constants to avoid depending on the full
// `@opentelemetry/semantic-conventions` package at runtime. When we do adopt
// the full SDK these can be re-exported from the package in one place.
export const ATTR_DB_SYSTEM = "db.system";
export const ATTR_DB_OPERATION = "db.operation";
export const ATTR_DB_STATEMENT = "db.statement";

export const ATTR_MESSAGING_SYSTEM = "messaging.system";
export const ATTR_MESSAGING_DESTINATION = "messaging.destination.name";
export const ATTR_MESSAGING_OP = "messaging.operation";

export const ATTR_CODE_FUNCTION = "code.function";
export const ATTR_CODE_NAMESPACE = "code.namespace";

export const ATTR_SERVICE_NAME = "service.name";
export const ATTR_SERVICE_VERSION = "service.version";
export const ATTR_SERVICE_INSTANCE_ID = "service.instance.id";
export const ATTR_DEPLOYMENT_ENVIRONMENT = "deployment.environment";

export const ATTR_PROCESS_RUNTIME_NAME = "process.runtime.name";
export const ATTR_PROCESS_RUNTIME_VERSION = "process.runtime.version";

export const ATTR_HOST_OS_TYPE = "host.os.type";
export const ATTR_HOST_ARCH = "host.arch";

// Theo-specific — anything we need that doesn't have an OTel standard form.
export const ATTR_THEO_GATE = "theo.gate";
export const ATTR_THEO_MODEL = "theo.model";
export const ATTR_THEO_ROLE = "theo.role";
export const ATTR_THEO_GOAL_ID = "theo.goal.id";
export const ATTR_THEO_PROPOSAL_ID = "theo.proposal.id";
export const ATTR_THEO_TURN_CLASS = "theo.turn_class";
export const ATTR_THEO_EVENT_ID = "theo.event.id";
export const ATTR_THEO_EVENT_TYPE = "theo.event.type";
export const ATTR_THEO_EVENT_VERSION = "theo.event.version";
export const ATTR_THEO_MESSAGE_LENGTH = "theo.message.length";
export const ATTR_THEO_AUTONOMY_DOMAIN = "theo.autonomy.domain";
export const ATTR_THEO_DEGRADATION_LEVEL = "theo.degradation.level";
export const ATTR_THEO_TOKENS_INPUT = "theo.tokens.input";
export const ATTR_THEO_TOKENS_OUTPUT = "theo.tokens.output";
export const ATTR_THEO_COST_USD = "theo.cost.usd";

/** Domain value used on bus dispatch spans. */
export const MESSAGING_SYSTEM_THEO = "theo.eventbus";

/** The canonical set of keys this module publishes, for tests. */
export const ALL_ATTRIBUTE_KEYS: readonly string[] = [
	ATTR_DB_SYSTEM,
	ATTR_DB_OPERATION,
	ATTR_DB_STATEMENT,
	ATTR_MESSAGING_SYSTEM,
	ATTR_MESSAGING_DESTINATION,
	ATTR_MESSAGING_OP,
	ATTR_CODE_FUNCTION,
	ATTR_CODE_NAMESPACE,
	ATTR_SERVICE_NAME,
	ATTR_SERVICE_VERSION,
	ATTR_SERVICE_INSTANCE_ID,
	ATTR_DEPLOYMENT_ENVIRONMENT,
	ATTR_PROCESS_RUNTIME_NAME,
	ATTR_PROCESS_RUNTIME_VERSION,
	ATTR_HOST_OS_TYPE,
	ATTR_HOST_ARCH,
	ATTR_THEO_GATE,
	ATTR_THEO_MODEL,
	ATTR_THEO_ROLE,
	ATTR_THEO_GOAL_ID,
	ATTR_THEO_PROPOSAL_ID,
	ATTR_THEO_TURN_CLASS,
	ATTR_THEO_EVENT_ID,
	ATTR_THEO_EVENT_TYPE,
	ATTR_THEO_EVENT_VERSION,
	ATTR_THEO_MESSAGE_LENGTH,
	ATTR_THEO_AUTONOMY_DOMAIN,
	ATTR_THEO_DEGRADATION_LEVEL,
	ATTR_THEO_TOKENS_INPUT,
	ATTR_THEO_TOKENS_OUTPUT,
	ATTR_THEO_COST_USD,
] as const;
