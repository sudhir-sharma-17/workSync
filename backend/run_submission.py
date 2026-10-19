import asyncio
import sys
import os
import argparse
import logging

# Add parent and local directories to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.db.session import AsyncSessionLocal
from app.db.models import UploadBatch, AttendanceRecord, WorkerMapping
from app.automation.submission_engine import PlaywrightSubmissionEngine
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("run_submission")

async def run_submission(batch_id: str, mode: str, session_id: str):
    logger.info(f"Starting submission for batch {batch_id} in mode {mode}...")
    async with AsyncSessionLocal() as session:
        batch = await session.get(UploadBatch, batch_id)
        if not batch:
            logger.error(f"Batch {batch_id} not found. Aborting.")
            return

        batch.status = "Running"
        await session.commit()

        # Fetch pending records with worker info
        records_res = await session.execute(
            select(AttendanceRecord)
            .options(selectinload(AttendanceRecord.worker))
            .where(
                AttendanceRecord.batch_id == batch_id,
                AttendanceRecord.status == "Pending",
            )
            .order_by(AttendanceRecord.attendance_date)
        )
        pending = records_res.scalars().all()

        if not pending:
            logger.info("No pending records to process.")
            batch.status = "Completed"
            await session.commit()
            return

        if mode in ("browser_validation", "browser_dry_run"):
            mode = "dry_run"

        if mode == "test":
            cap = 5
            pending = pending[:cap]

        # Synchronize pending records with confirmed project mappings from batch.debug_meta
        if batch.debug_meta:
            try:
                import json
                from sqlalchemy import update
                debug_data = json.loads(batch.debug_meta)
                resolutions = debug_data.get("project_resolution", [])
                mapping_dict = {}
                for r in resolutions:
                    inp = (r.get("input_project") or "").strip().lower()
                    resolved = (r.get("resolved_project") or "").strip()
                    if inp and resolved and (r.get("confidence", 0) >= 80 or r.get("status") == "Auto-Accepted"):
                        mapping_dict[inp] = resolved

                updated_any = False
                for rec in pending:
                    cur_proj = (rec.project_name or "").strip()
                    if cur_proj.lower() in mapping_dict:
                        canonical = mapping_dict[cur_proj.lower()]
                        if rec.project_name != canonical:
                            rec.project_name = canonical
                            updated_any = True
                if updated_any:
                    await session.flush()
            except Exception as e:
                logger.warning(f"Error syncing debug_meta project mappings in run_submission: {e}")

        # Bulk-fetch BOQ mappings
        mappings_res = await session.execute(select(WorkerMapping).where(WorkerMapping.session_id == session_id))
        boq_map = {m.worker_type: m for m in mappings_res.scalars().all()}

        # Build serialisable records list
        records_payload = []
        for rec in pending:
            worker = rec.worker
            mapping = boq_map.get(worker.worker_type) if worker else None
            desc_val = rec.custom_description if rec.custom_description is not None else (mapping.description if mapping else "")
            
            records_payload.append({
                "id": str(rec.id),
                "attendance_date": rec.attendance_date.strftime("%d/%m") if rec.attendance_date else "",
                "worker_name": worker.name if worker else "",
                "worker_type": worker.worker_type if worker else "",
                "project_name": rec.project_name or "",
                "boq_category": mapping.boq_category if mapping else "",
                "description": desc_val,
                "duration": rec.duration or "",
            })

        engine = PlaywrightSubmissionEngine(
            db_session=session,
            batch_id=batch_id,
            session_id=session_id,
            mode=mode,
        )
        try:
            await engine.start_batch(records_payload, batch.form_url)
            await session.refresh(batch)
            if batch.status not in ("Cancelled", "Failed"):
                batch.status = "Completed"
                await session.commit()
            logger.info(f"Batch {batch_id} completed successfully.")
        except Exception as exc:
            logger.exception(f"Batch {batch_id} crashed: {exc}")
            await session.refresh(batch)
            batch.status = "Failed"
            await session.commit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()

    asyncio.run(run_submission(args.batch_id, args.mode, args.session_id))
