"""Evidence-driven, coalesced background work; no perpetual inference loop."""

from theo.domain import Json, digest, encode, uid
from theo.jobs import Jobs
from theo.storage import Database

CADENCES = {
    "proactive_scan": 3 * 3600,
    "deep_work": 2 * 3600,
    "reflection": 7 * 86400,
    "reflexion": 6 * 3600,
    "dream": 6 * 3600,
    "episode_consolidation": 6 * 3600,
    "insight_consolidation": 7 * 86400,
    "feedback_consolidation": 30 * 86400,
    "lifecycle_review": 30 * 86400,
    "skill_extraction": 6 * 3600,
    "plan_momentum": 3 * 3600,
}

INSTRUCTIONS = {
    "reflection": "Find a specific repeated outcome pattern and propose one evidence-backed improvement with a regression check.",
    "reflexion": "Explain this unaddressed failure mechanism and propose a narrow guard/test. Retrieve existing lessons first. Do not duplicate them.",
    "dream": "Suggest a useful speculative connection between these memories. Label it speculation; save a proposal, never a fact. Do not contact the owner merely to ask for attention.",
    "episode_consolidation": "Consolidate related episodes with source IDs; preserve originals and revisions. Store the summary as an inference.",
    "insight_consolidation": "Reassess related insights against evidence; propose one attributable synthesis without replacing sources.",
    "feedback_consolidation": "Distinguish explicit preference from weak engagement signals. Propose a preference adjustment supported by repeated evidence.",
    "skill_extraction": "Extract a narrow reusable procedure from repeated demonstrated successes. Include triggers, least grants, test cases and rollback criteria. Submit a proposed skill; do not activate it.",
    "deep_work": "Advance the next executable goal step. Produce a registered artifact or observable change, record evidence and next action. Size alone is not a blocker.",
}


class Autonomy:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    async def opportunity(self, kind: str) -> Json:
        if kind not in CADENCES:
            raise ValueError("Unknown autonomy behavior")
        if await self.db.control(self.owner, "background_paused") == "true":
            return {"status": "noop", "reason": "background_paused"}
        if kind == "deep_work":
            goals = await self.db.read(
                "SELECT g.id goal_id,g.title,g.criteria,s.id step_id,s.next_action FROM goals g JOIN plan_steps s ON s.goal_id=g.id WHERE g.owner_id=? AND g.status='active' AND s.status IN ('pending','active') AND NOT EXISTS(SELECT 1 FROM step_dependencies d JOIN plan_steps p ON p.id=d.depends_on WHERE d.step_id=s.id AND p.status<>'completed') ORDER BY g.updated_at,s.ordinal LIMIT 1",
                (self.owner,),
            )
            return (
                self._work(kind, goals)
                if goals
                else {"status": "noop", "reason": "no_executable_goal"}
            )
        if kind == "proactive_scan":
            commitments = await self.db.read(
                "SELECT * FROM commitments WHERE owner_id=? AND status='active' AND due_at<=?",
                (self.owner, self.db.clock() + 86400),
            )
            if not commitments:
                return {"status": "noop", "reason": "no_actionable_commitment"}
            return {
                "status": "proposal",
                "kind": kind,
                "evidence": commitments,
                "body": "Review these due commitments and prepare their next concrete action.",
            }
        if kind == "plan_momentum":
            goals = await self.db.read(
                "SELECT * FROM goals WHERE owner_id=? AND status='active'", (self.owner,)
            )
            delayed: list[Json] = []
            for goal in goals:
                row = await self.db.one(
                    "SELECT sum(max(0,min(COALESCE(ended_at,heartbeat_at),?)-max(started_at,?))) seconds FROM lifecycle_intervals WHERE owner_id=? AND deliberate_pause=0 AND COALESCE(ended_at,heartbeat_at)>?",
                    (self.db.clock(), goal["updated_at"], self.owner, goal["updated_at"]),
                )
                observed = float(row["seconds"] or 0) if row else 0
                if observed >= 48 * 3600:
                    delayed.append(
                        {
                            "goal_id": goal["id"],
                            "observed_hours": observed / 3600,
                            "alert_eligible": observed >= 96 * 3600,
                        }
                    )
            return (
                {
                    "status": "proposal",
                    "kind": kind,
                    "evidence": delayed,
                    "body": "Resume the next executable step; batch alerts only for 96 observed running hours.",
                }
                if delayed
                else {"status": "noop", "reason": "no_observed_stall"}
            )
        if kind == "lifecycle_review":
            candidates = await self.db.read(
                "SELECT id,revision,kind,updated_at FROM memory_records WHERE owner_id=? AND status='active' AND pinned=0 AND kind NOT IN ('goal','preference') AND updated_at<? LIMIT 50",
                (self.owner, self.db.clock() - 90 * 86400),
            )
            return (
                {
                    "status": "proposal",
                    "kind": kind,
                    "evidence": candidates,
                    "body": "Review staleness and exposure before archival; no deletion or importance increase is automatic.",
                }
                if candidates
                else {"status": "noop", "reason": "no_stale_candidates"}
            )
        if kind == "dream":
            engagement = await self.db.one(
                "SELECT max(last_engagement) latest FROM conversations WHERE owner_id=?",
                (self.owner,),
            )
            if (
                engagement
                and engagement["latest"]
                and self.db.clock() - engagement["latest"] < 7200
            ):
                return {"status": "noop", "reason": "quiet_period_not_reached"}
        if kind in ("reflection", "reflexion", "skill_extraction"):
            status = "failed" if kind == "reflexion" else "completed"
            evidence = await self.db.read(
                "SELECT id,job_id,status,error,output FROM runs WHERE owner_id=? AND status=? ORDER BY started_at DESC LIMIT 20",
                (self.owner, status),
            )
            if kind == "skill_extraction":
                evidence = [row for row in evidence if row["output"]]
                if len(evidence) < 3:
                    return {"status": "noop", "reason": "insufficient_repeated_success"}
        elif kind == "feedback_consolidation":
            evidence = await self.db.read(
                "SELECT id,kind,body,explicit FROM feedback WHERE owner_id=? ORDER BY created_at DESC LIMIT 30",
                (self.owner,),
            )
        else:
            memory_kind = (
                "episode"
                if kind == "episode_consolidation"
                else "insight"
                if kind == "insight_consolidation"
                else "entity"
            )
            evidence = await self.db.read(
                "SELECT m.id,m.revision,r.body,r.source FROM memory_records m JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.revision WHERE m.owner_id=? AND m.status='active' AND m.kind=? ORDER BY m.updated_at DESC LIMIT 10",
                (self.owner, memory_kind),
            )
        if not evidence:
            return {"status": "noop", "reason": "no_new_evidence"}
        return self._work(kind, evidence)

    @staticmethod
    def _work(kind: str, evidence: list[Json]) -> Json:
        return {
            "status": "work",
            "kind": kind,
            "evidence": evidence,
            "text": INSTRUCTIONS[kind] + "\nEvidence: " + encode(evidence),
        }

    async def tick(self, conversation: str) -> list[Json]:
        if await self.db.control(self.owner, "background_paused") == "true":
            return []
        reports: list[Json] = []
        for kind, cadence in CADENCES.items():
            last = float(await self.db.control(self.owner, "autonomy_last:" + kind) or 0)
            if self.db.clock() - last < cadence:
                continue
            result = await self.opportunity(kind)
            reports.append({"kind": kind, **result})
            await self.db.set_control(self.owner, "autonomy_last:" + kind, str(self.db.clock()))
            if result["status"] == "noop":
                await self.db.health(
                    self.owner, "autonomy_noop", {"kind": kind, "reason": result["reason"]}
                )
                continue
            source_key = digest(result["evidence"])
            if result["status"] == "proposal":
                await self.db.execute(
                    "INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        self.owner,
                        kind,
                        source_key,
                        encode(result["evidence"]),
                        result["body"],
                        "proposed",
                        None,
                        self.db.clock(),
                    ),
                )
            else:
                # Evidence identity deduplicates already-addressed failures and repeated scans.
                key = f"autonomy:{kind}:{source_key}"
                await Jobs(self.db, self.owner).enqueue(
                    conversation,
                    kind,
                    {"text": result["text"], "evidence": result["evidence"]},
                    key,
                    deadline=self.db.clock() + (5400 if kind == "deep_work" else 1800),
                )
        return reports

    async def record_proposal(self, kind: str, evidence: Json, body: str) -> str:
        proposal_id = uid()
        await self.db.execute(
            "INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                self.owner,
                kind,
                digest(evidence),
                encode(evidence),
                body,
                "proposed",
                None,
                self.db.clock(),
            ),
        )
        row = await self.db.one(
            "SELECT id FROM proposals WHERE owner_id=? AND kind=? AND source_key=?",
            (self.owner, kind, digest(evidence)),
        )
        assert row
        return str(row["id"])
