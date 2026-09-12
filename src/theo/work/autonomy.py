"""Admit evidence-driven background work on bounded cadences.

Coalesces source changes into durable work or review proposals with deduplication
and explicit no-op reasons; it does not run a continuous inference loop.
"""

import sqlite3

from theo.domain import Json, digest, encode, uid
from theo.privacy import group_scope
from theo.storage import Database
from theo.work.jobs import Jobs

CADENCES = {
    "proactive_scan": 15 * 60,
    "deep_work": 60,
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
    "proactive_scan": "Review the person's recent conversation, current goals and attention items. Notice an unfinished request, a meaningful personal development, a useful connection, a changed blocker or timely preparation you can do on their behalf. Recheck current state and prior work before acting. Advance accepted goals, repair stale next actions, and persist useful preferences or insights with sources. A technical uncertainty calls for investigation through available permitted tools, not another request to repeat permission. You may take a small reversible next step within an established direction; a new major goal needs the person's direction. Do not revive completed work or create duplicate tasks. Care extends beyond work, but never invent a personal concern, treat silence as assent, or contact the person just to solicit engagement. If no worthwhile action is supported, stay quiet.",
    "reflection": "Find a specific repeated outcome pattern and propose one evidence-backed improvement with a regression check.",
    "reflexion": "Explain this unaddressed failure mechanism and propose a narrow guard/test. Retrieve existing lessons first. Do not duplicate them.",
    "dream": "Suggest a useful speculative connection between these memories. Label it speculation; save a proposal, never a fact. Do not contact the owner merely to ask for attention.",
    "episode_consolidation": "Consolidate related episodes with source IDs; preserve originals and revisions. Store the summary as an inference.",
    "insight_consolidation": "Reassess related insights against evidence; propose one attributable synthesis without replacing sources.",
    "feedback_consolidation": "Distinguish explicit preference from weak engagement signals. Propose a preference adjustment supported by repeated evidence.",
    "skill_extraction": "Extract a narrow reusable procedure from repeated demonstrated successes. Include triggers, least grants, test cases and rollback criteria. Submit a proposed skill; do not activate it.",
    "deep_work": "Own the goal through completion. Inspect the current plan and promised checkpoints. Work through a substantial bounded batch of related material, not one tiny file per run. Produce actual saved results or artifacts. If unfinished, revise the next action with an exact coverage cursor and concrete remaining work; the goal runner admits that continuation automatically, so do not also schedule a duplicate work job. Persist a goal_checkpoint before promising a progress-update time. Give a completion estimate only with a measured scope and pace; otherwise establish one at the checkpoint. Keep per-batch notes internal. Send meaningful milestones, promised updates and the finished result without waiting for the owner to ask. A partial result is not goal completion. Size alone is not a blocker.",
}


class Autonomy:
    def __init__(self, db: Database, owner: str):
        self.db, self.owner = db, owner

    async def opportunity(self, kind: str) -> Json:
        if kind not in CADENCES:
            raise ValueError("Unknown autonomy behavior")
        if await self.db.control(self.owner, "autonomy_paused") == "true":
            return {"status": "noop", "reason": "background_paused"}
        if kind == "deep_work":
            goals = await self.db.read(
                "SELECT g.id goal_id,g.conversation_id,g.title,g.criteria,s.id step_id,s.next_action FROM goals g JOIN plan_steps s ON s.goal_id=g.id WHERE g.owner_id=? AND g.status='active' AND s.status IN ('pending','active') AND NOT EXISTS(SELECT 1 FROM step_dependencies d JOIN plan_steps p ON p.id=d.depends_on WHERE d.step_id=s.id AND p.status<>'completed') ORDER BY g.updated_at,s.ordinal LIMIT 20",
                (self.owner,),
            )
            for goal in goals:
                key = f"autonomy:{kind}:{digest([goal])}"
                if not await self.db.one(
                    "SELECT 1 FROM jobs WHERE owner_id=? AND semantic_key=?",
                    (self.owner, key),
                ):
                    return self._work(kind, [goal])
            return {"status": "noop", "reason": "no_new_executable_goal_step"}
        if kind == "proactive_scan":
            return await self._proactive()
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

    async def _proactive(self) -> Json:
        """Use changing personal context, not an unwritten commitments table alone."""
        recent = await self.db.read(
            "SELECT m.id,m.conversation_id,m.sequence,substr(m.content,1,1500) content,substr(m.parts,1,1500) parts,m.created_at FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.owner_id=? AND m.role='user' AND m.content NOT LIKE '/%' AND m.created_at>=? AND (c.channel='local' OR EXISTS(SELECT 1 FROM telegram_destinations t WHERE t.conversation_id=c.id AND t.private=1)) ORDER BY m.created_at DESC,m.id DESC LIMIT 12",
            (self.owner, self.db.clock() - 7 * 86400),
        )
        if recent and self.db.clock() - recent[0]["created_at"] < 120:
            return {"status": "noop", "reason": "conversation_in_progress"}
        commitments = await self.db.read(
            "SELECT * FROM commitments WHERE owner_id=? AND status='active' AND due_at<=? ORDER BY due_at,id LIMIT 20",
            (self.owner, self.db.clock() + 86400),
        )
        pins = await self.db.read(
            "SELECT id,substr(body,1,1000) body,expires_at FROM attention_pins WHERE owner_id=? AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC,id LIMIT 20",
            (self.owner, self.db.clock()),
        )
        # New user messages or completed host actions can resolve a recorded blocker.
        # Include their identities so an unchanged failure never produces an endless retry.
        blocked = await self.db.read(
            "SELECT id,conversation_id,title,blocker,updated_at FROM goals WHERE owner_id=? AND status='blocked' ORDER BY updated_at DESC,id LIMIT 10",
            (self.owner,),
        )
        receipts = (
            await self.db.read(
                "SELECT id,conversation_id,operation,status,updated_at FROM actions WHERE owner_id=? AND operation='host_command' AND status='succeeded' AND updated_at>=? ORDER BY updated_at DESC,id LIMIT 10",
                (self.owner, self.db.clock() - 86400),
            )
            if blocked
            else []
        )
        evidence = [
            {
                "recent_messages": recent,
                "commitments": commitments,
                "attention": pins,
                "blocked_goals": blocked,
                "completed_host_actions": receipts,
            }
        ]
        if not any((recent, commitments, pins, blocked)):
            return {"status": "noop", "reason": "no_actionable_context"}
        return self._work("proactive_scan", evidence)

    async def tick(self, conversation: str) -> list[Json]:
        if await group_scope(self.db, conversation):
            return []  # Owner-wide evidence must never be assembled in a group.
        if await self.db.control(self.owner, "autonomy_paused") == "true":
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
                if await self.db.one(
                    "SELECT 1 FROM jobs WHERE owner_id=? AND kind=? AND status IN ('queued','running','waiting_for_auth','waiting_for_quota','interrupted') LIMIT 1",
                    (self.owner, kind),
                ):
                    continue
                # Evidence identity deduplicates already-addressed failures and repeated scans.
                key = f"autonomy:{kind}:{source_key}"
                await self._admit_work(conversation, kind, result, key)
        return reports

    async def _admit_work(self, conversation: str, kind: str, result: Json, key: str) -> None:
        """Consume evidence once, including jobs hydrated by older runtimes."""

        def admit(db: sqlite3.Connection) -> None:
            # Admission and the evidence check share the writer transaction. Execution
            # may enrich a payload; it must not make consumed evidence a new request.
            if db.execute(
                "SELECT 1 FROM jobs WHERE owner_id=? AND semantic_key=?",
                (self.owner, key),
            ).fetchone():
                return
            Jobs(self.db, self.owner).insert(
                db,
                result["evidence"][0]["conversation_id"] if kind == "deep_work" else conversation,
                kind,
                {"text": result["text"], "evidence": result["evidence"]},
                key,
                deadline=self.db.clock() + (5400 if kind == "deep_work" else 1800),
                origin="autonomous",
            )

        await self.db.write(admit)

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
