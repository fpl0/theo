/**
 * Thin wrapper around `cron-parser` (v5).
 *
 * We keep the surface tiny: compute the next fire instant after a given
 * moment, and validate an expression without throwing. `cron-parser`'s v5
 * API entrypoint is `CronExpressionParser.parse(expr, { currentDate })`;
 * the resulting `CronExpression` exposes `.next()` returning a `CronDate`
 * with `.toDate()` — see `node_modules/cron-parser/dist/types/` for full
 * shape. Five-field (standard) crontab only; seconds are not used.
 */

import { CronExpressionParser } from "cron-parser";

/**
 * Compute the next run instant strictly after `from`. `cron-parser` treats
 * `currentDate` as exclusive, so calling `.next()` returns the first match
 * after `from` even when `from` itself is a match.
 *
 * Throws if the expression is malformed; callers should validate with
 * `isValidCron` before persisting.
 */
export function nextRun(expression: string, from: Date): Date {
	const expr = CronExpressionParser.parse(expression, { currentDate: from });
	return expr.next().toDate();
}

/**
 * Validate a cron expression without throwing. Returns true if the expression
 * parses cleanly, false otherwise.
 */
export function isValidCron(expression: string): boolean {
	try {
		CronExpressionParser.parse(expression);
		return true;
	} catch {
		return false;
	}
}
