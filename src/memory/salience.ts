/**
 * Episode salience.
 *
 * Episodes whose `importance >= CONSOLIDATION_GATE` are exempt from the
 * background compressor and preserved at full fidelity. The gate is set
 * at 0.8 so that a single strong signal (owner marker, core update) does
 * not cross it on its own — at least two strong signals together do.
 *
 * The scoring function that assigns these values lives with the engine
 * once episode finalization is wired (Phase 14); the gate constant is
 * exposed here so consolidation can import it standalone.
 */

export const CONSOLIDATION_GATE = 0.8;
