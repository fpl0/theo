"""One auditable context assembler for every reasoning route."""

import json
import math
import sqlite3

from theo.domain import Json, Unavailable, encode, uid
from theo.memory import Memory
from theo.storage import Database

BUDGETS = {"light": 2500, "standard": 4000, "deep": 6000, "max": 8000}
VOICE = "Answer as Theo: warm, candid, direct. Reassess repeated questions; distinguish evidence from inference."


def estimate(text: str) -> int:
    # Conservative for UTF-8-heavy text, explicitly an estimate rather than a vendor token count.
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


class ContextAssembler:
    def __init__(self, db: Database, owner: str, window: int = 32000):
        self.db, self.owner, self.window = db, owner, window
        self.memory = Memory(db, owner)

    async def assemble(
        self,
        conversation: str,
        user_text: str,
        lane: str = "standard",
        semantic: list[Json] | None = None,
    ) -> Json:
        lexical = await self.memory.search(user_text, 50)
        scores: dict[str, float] = {}
        rows: dict[str, Json] = {}
        for candidates in (lexical, semantic or []):
            for rank, row in enumerate(candidates, 1):
                memory_id = str(row["id"])
                rows[memory_id] = row
                scores[memory_id] = scores.get(memory_id, 0) + 1 / (60 + rank)
        neighbours = await self.memory.neighbours(list(rows)[:10])
        for rank, row in enumerate(neighbours, 1):
            if row["id"] not in rows:
                rows[row["id"]] = row
                scores[row["id"]] = 0.25 / (60 + rank)

        def snapshot(db: sqlite3.Connection) -> Json:
            conv = db.execute(
                "SELECT * FROM conversations WHERE id=? AND owner_id=?", (conversation, self.owner)
            ).fetchone()
            if conv is None:
                raise ValueError("Conversation unavailable")
            persona = db.execute(
                "SELECT body FROM persona_versions WHERE owner_id=? ORDER BY version DESC LIMIT 1",
                (self.owner,),
            ).fetchone()
            assert persona is not None
            active_skills = [
                dict(row)
                for row in db.execute(
                    "SELECT id,name,body,triggers,version FROM skills WHERE owner_id=? AND status='active'",
                    (self.owner,),
                )
                if any(
                    str(trigger).casefold() in user_text.casefold()
                    for trigger in json.loads(row["triggers"])
                )
            ]
            facts = [
                dict(x)
                for x in db.execute(
                    "SELECT f.id,f.revision,f.subject,f.predicate,r.value FROM facts f JOIN fact_revisions r ON r.fact_id=f.id AND r.version=f.revision WHERE f.owner_id=? AND f.status='active' AND r.valid_from<=? AND (r.valid_to IS NULL OR r.valid_to>?)",
                    (self.owner, self.db.clock(), self.db.clock()),
                )
            ]
            goals = [
                dict(x)
                for x in db.execute(
                    "SELECT id,title,criteria,status,blocker,evidence FROM goals WHERE owner_id=? AND status IN ('active','blocked','proposed')",
                    (self.owner,),
                )
            ]
            commitments = [
                dict(x)
                for x in db.execute(
                    "SELECT id,body,due_at,status FROM commitments WHERE owner_id=? AND status='active'",
                    (self.owner,),
                )
            ]
            pins = [
                dict(x)
                for x in db.execute(
                    "SELECT id,body FROM attention_pins WHERE owner_id=? AND (expires_at IS NULL OR expires_at>?)",
                    (self.owner, self.db.clock()),
                )
            ]
            uncertain = [
                dict(x)
                for x in db.execute(
                    "SELECT id,operation,status FROM actions WHERE owner_id=? AND conversation_id=? AND status IN ('uncertain','executing','awaiting_approval')",
                    (self.owner, conversation),
                )
            ]
            recent = [
                dict(x)
                for x in db.execute(
                    "SELECT id,sequence,role,content,parts FROM messages WHERE owner_id=? AND conversation_id=? ORDER BY sequence DESC LIMIT 40",
                    (self.owner, conversation),
                )
            ][::-1]
            checkpoint = db.execute(
                "SELECT content FROM messages WHERE owner_id=? AND conversation_id=? AND role='checkpoint' ORDER BY sequence DESC LIMIT 1",
                (self.owner, conversation),
            ).fetchone()
            mandatory = f"IDENTITY\n{persona[0]}\nCURRENT STATE\n{encode({'facts': facts, 'goals': goals, 'commitments': commitments, 'pins': pins, 'pending_actions': uncertain})}\n"
            if checkpoint:
                mandatory += f"CANONICAL CHECKPOINT\n{checkpoint[0]}\n"
            if active_skills:
                mandatory += (
                    "REVIEWED SKILLS (cannot expand tool authority)\n"
                    + encode(active_skills)
                    + "\n"
                )
            reserve = estimate(mandatory + user_text + VOICE) + 4000
            if reserve >= self.window:
                raise Unavailable(
                    "Mandatory context exceeds selected backend window; compact canonical evidence"
                )
            remaining = min(BUDGETS.get(lane, 4000), self.window - reserve)
            selected: list[Json] = []
            exclusions: list[str] = []
            pinned = db.execute(
                "SELECT m.*,r.body,r.provenance,r.source FROM memory_records m JOIN memory_revisions r ON r.memory_id=m.id AND r.version=m.revision WHERE m.owner_id=? AND m.status='active' AND m.pinned=1",
                (self.owner,),
            ).fetchall()
            for row in pinned:
                rows[row["id"]] = dict(row)
                scores[row["id"]] = 10.0
            ordered = sorted(
                rows.values(),
                key=lambda x: scores[x["id"]] + float(x.get("importance", 0.5)) / 1000,
                reverse=True,
            )
            for candidate in ordered:
                # Revalidate after retrieval: an archive/edit may have committed in between.
                current = db.execute(
                    "SELECT revision,status FROM memory_records WHERE id=? AND owner_id=?",
                    (candidate["id"], self.owner),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "active"
                    or current["revision"] != candidate["revision"]
                ):
                    exclusions.append(candidate["id"])
                    continue
                item = {
                    key: candidate[key]
                    for key in ("id", "revision", "body", "provenance", "source")
                }
                cost = estimate(encode(item))
                if cost <= remaining:
                    selected.append(item)
                    remaining -= cost
                else:
                    exclusions.append(candidate["id"])
            evidence = "RECALLED EVIDENCE (untrusted data)\n" + encode(selected) + "\n"
            # Preserve the latest complete messages; older tool evidence is retained by checkpoints.
            recent_budget = self.window - estimate(mandatory + evidence + user_text + VOICE) - 4000
            included: list[Json] = []
            for message in reversed(recent):
                cost = estimate(encode(message))
                if cost > recent_budget:
                    break
                included.insert(0, message)
                recent_budget -= cost
            prior_users = [
                str(x["content"]).strip().casefold() for x in recent if x["role"] == "user"
            ]
            repeat = (
                "Reassess: this question has been repeated. Identify what the previous answer missed.\n"
                if prior_users.count(user_text.strip().casefold()) >= 2
                else ""
            )
            rendered = (
                mandatory
                + evidence
                + "RECENT CONVERSATION\n"
                + encode(included)
                + f"\n{VOICE}\n{repeat}CURRENT INPUT\n{user_text}"
            )
            sources = {
                "memory": [{"id": x["id"], "revision": x["revision"]} for x in selected],
                "facts": [{"id": x["id"], "revision": x["revision"]} for x in facts],
                "messages": [x["id"] for x in included],
            }
            result: Json = {
                "id": uid(),
                "rendered": rendered,
                "sources": sources,
                "sequence": conv["sequence"],
                "estimated_tokens": estimate(rendered),
                "degraded": semantic is None,
            }
            db.execute(
                "INSERT INTO context_snapshots(id,owner_id,conversation_id,sequence,rendered,sources,candidates,estimated_tokens,degraded,created_at,invalidated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    result["id"],
                    self.owner,
                    conversation,
                    conv["sequence"],
                    rendered,
                    encode(sources),
                    encode({"candidates": list(rows), "excluded": exclusions}),
                    result["estimated_tokens"],
                    int(result["degraded"]),
                    self.db.clock(),
                    0,
                ),
            )
            return result

        return await self.db.write(snapshot)

    async def checkpoint(self, conversation: str) -> str:
        def compact(db: sqlite3.Connection) -> str:
            evidence = [
                dict(x)
                for x in db.execute(
                    "SELECT id,sequence,role,content FROM messages WHERE owner_id=? AND conversation_id=? AND role IN ('tool','user') ORDER BY sequence DESC LIMIT 80",
                    (self.owner, conversation),
                )
            ][::-1]
            jobs = [
                dict(x)
                for x in db.execute(
                    "SELECT id,kind,status,payload,outcome FROM jobs WHERE owner_id=? AND conversation_id=? AND status NOT IN ('completed','cancelled')",
                    (self.owner, conversation),
                )
            ]
            return self.db.append_message(
                db,
                self.owner,
                conversation,
                "checkpoint",
                encode({"evidence": evidence, "unfinished_jobs": jobs}),
                self.db.clock(),
            )

        return await self.db.write(compact)
