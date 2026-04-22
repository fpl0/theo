/**
 * Call `.unref()` on a timer handle when the runtime supports it. Bun and
 * Node both do, but their return types diverge; this helper papers over
 * the difference without a biome-ignored cast at every call site.
 * Without unref, `bun test` hangs on pending interval/timeout handles.
 */
export function unrefTimer(handle: unknown): void {
	if (typeof handle !== "object" || handle === null) return;
	const unref = (handle as { unref?: () => void }).unref;
	if (typeof unref === "function") unref.call(handle);
}
