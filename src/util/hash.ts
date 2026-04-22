/**
 * SHA-256 helper returning lowercase hex. Accepts either bytes or strings
 * (strings are UTF-8 encoded). The implementation uses the standard Web
 * Crypto API so it runs the same on Bun and Node.
 */

export async function sha256Hex(input: ArrayBuffer | string): Promise<string> {
	const bytes = typeof input === "string" ? new TextEncoder().encode(input) : input;
	const digest = await crypto.subtle.digest("SHA-256", bytes);
	const view = new Uint8Array(digest);
	let out = "";
	for (const byte of view) {
		out += byte.toString(16).padStart(2, "0");
	}
	return out;
}
