"""
POST /api/automation/validate          — open form, detect fields, return report
POST /api/automation/run               — kick off real Playwright submissions
GET  /api/automation/status/{batch_id} — poll live submission progress
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional

from app.api.deps import get_db, get_session_id
from app.db.models import (
    UploadBatch, AttendanceRecord, WorkerMapping, SubmissionResult,
)
from app.automation.submission_engine import validate_form, PlaywrightSubmissionEngine, check_google_session_status

router = APIRouter(prefix="/automation", tags=["Automation"])
logger = logging.getLogger(__name__)

# Dedicated thread pool for Playwright.
# Each submitted job runs its own asyncio.run() → creates a fresh ProactorEventLoop
# on Windows, which supports subprocess creation (unlike uvicorn's SelectorEventLoop).
_playwright_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="playwright")
_active_connections = set()


# ── Request schemas ──────────────────────────────────────────────────────────

class ValidateRequest(BaseModel):
    form_url: str


class RunRequest(BaseModel):
    batch_id: str
    mode: str = "test"          # "dry_run" | "test" | "production"
    limit: Optional[int] = None  # max records in test mode (default 5)


# ── Background task helper ───────────────────────────────────────────────────

async def _run_playwright_background(
    batch_id: str,
    records_payload: list,
    form_url: str,
    mode: str,
    session_id: str,
):
    """
    Async implementation — called inside a fresh asyncio.run() from the thread pool.
    Creates its own DB session outside the HTTP request lifecycle.
    """
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        batch = await session.get(UploadBatch, batch_id)
        if not batch:
            logger.error(f"[BG] Batch {batch_id} not found. Aborting.")
            return

        batch.status = "Running"
        await session.commit()

        engine = PlaywrightSubmissionEngine(
            db_session=session,
            batch_id=batch_id,
            session_id=session_id,
            mode=mode,
        )
        try:
            await engine.start_batch(records_payload, form_url)
            await session.refresh(batch)
            if batch.status not in ("Cancelled", "Failed"):
                batch.status = "Completed"
                await session.commit()
            logger.info(f"[BG] Batch {batch_id} completed.")
        except Exception as exc:
            logger.exception(f"[BG] Batch {batch_id} crashed: {exc}")
            await session.refresh(batch)
            batch.status = "Failed"
            await session.commit()


def _sync_playwright_background(batch_id: str, records_payload: list, form_url: str, mode: str, session_id: str):
    """
    Sync wrapper executed in the thread pool.
    asyncio.run() creates a fresh ProactorEventLoop on Windows — Playwright compatible.
    """
    asyncio.run(_run_playwright_background(batch_id, records_payload, form_url, mode, session_id))


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/google/status")
async def get_google_status(session_id: str = Depends(get_session_id)):
    from app.automation.submission_engine import is_connection_active
    if is_connection_active(session_id):
        return {"connected": False, "email": None, "connecting": True}
        
    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(
        _playwright_pool,
        lambda: asyncio.run(check_google_session_status(session_id)),
    )
    status["connecting"] = False
    return status


@router.post("/google/connect")
async def connect_google(session_id: str = Depends(get_session_id)):
    import os
    from playwright.async_api import async_playwright
    from app.automation.submission_engine import mark_connection_active, mark_connection_inactive, is_connection_active
    
    if is_connection_active(session_id):
        return {"connected": False, "email": None, "connecting": True}
        
    session_dir = os.path.abspath(f"playwright_sessions/{session_id}")
    os.makedirs(session_dir, exist_ok=True)
    
    async def run_headed_login_bg():
        mark_connection_active(session_id)
        try:
            async with async_playwright() as p:
                try:
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir=session_dir,
                        headless=False,
                        ignore_default_args=["--enable-automation"],
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-infobars",
                        ],
                    )
                except Exception as e:
                    logger.error(f"Failed to launch persistent context for login: {e}")
                    return

                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto("https://accounts.google.com", wait_until="domcontentloaded", timeout=60000)
                    while len(context.pages) > 0 and not page.is_closed():
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error in headed login window: {e}")
                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass
        finally:
            mark_connection_inactive(session_id)
                
    loop = asyncio.get_running_loop()
    loop.run_in_executor(_playwright_pool, lambda: asyncio.run(run_headed_login_bg()))
    
    return {"connected": False, "email": None, "connecting": True}



@router.post("/google/disconnect")
async def disconnect_google(session_id: str = Depends(get_session_id)):
    import shutil
    import os
    import time
    import uuid
    session_dir = os.path.abspath(f"playwright_sessions/{session_id}")
    if os.path.exists(session_dir):
        deleted = False
        for _ in range(5):
            try:
                shutil.rmtree(session_dir)
                deleted = True
                break
            except Exception:
                await asyncio.sleep(0.5)
        
        if not deleted:
            try:
                trash_dir = os.path.abspath(f"playwright_sessions/trash_{uuid.uuid4().hex}")
                os.rename(session_dir, trash_dir)
                def delete_trash():
                    try:
                        shutil.rmtree(trash_dir)
                    except Exception:
                        pass
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, delete_trash)
            except Exception as e:
                logger.error(f"Failed to force disconnect session dir: {e}")
                raise HTTPException(status_code=500, detail="Failed to disconnect due to file locks. Please try again.")
    return {"connected": False, "email": None}


@router.post("/validate")
async def validate_google_form(
    req: ValidateRequest,
    session_id: str = Depends(get_session_id),
):
    """
    Open the Google Form URL with Playwright, detect all question labels,
    match them against the alias table, and return a structured report.
    Playwright runs in a thread pool to avoid Windows asyncio subprocess errors.
    """
    if not req.form_url or not req.form_url.startswith("http"):
        raise HTTPException(status_code=400, detail="A valid form URL is required.")

    loop = asyncio.get_running_loop()
    
    # Check Google account connection
    google_status = await loop.run_in_executor(
        _playwright_pool,
        lambda: asyncio.run(check_google_session_status(session_id)),
    )
    if not google_status["connected"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please connect a Google account that has access to this form."
        )

    # Run validate_form in a dedicated thread → fresh asyncio.run() → ProactorEventLoop
    report = await loop.run_in_executor(
        _playwright_pool,
        lambda: asyncio.run(validate_form(req.form_url, session_id)),
    )
    return report


@router.post("/run")
async def run_automation(
    req: RunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """
    Trigger real Playwright form submissions for a batch.
    The submission runs in the background; poll /status/{batch_id} for progress.

    Modes:
      dry_run    — count only, no real submissions
      test       — submit up to `limit` records (default 5)
      production — submit all pending records
    """
    # Verify batch ownership
    batch_res = await db.execute(
        select(UploadBatch).where(UploadBatch.id == req.batch_id)
    )
    batch = batch_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")
    if batch.session_id != session_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # Check Google account connection
    loop = asyncio.get_running_loop()
    google_status = await loop.run_in_executor(
        _playwright_pool,
        lambda: asyncio.run(check_google_session_status(session_id)),
    )
    if not google_status["connected"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please connect a Google account that has access to this form."
        )

    form_url = batch.form_url

    # Fetch pending records with worker info
    records_res = await db.execute(
        select(AttendanceRecord)
        .options(selectinload(AttendanceRecord.worker))
        .where(
            AttendanceRecord.batch_id == req.batch_id,
            AttendanceRecord.status == "Pending",
        )
        .order_by(AttendanceRecord.attendance_date)
    )
    pending = records_res.scalars().all()

    if not pending:
        return {
            "message": "No pending records to process.",
            "submitted": 0,
            "mode": req.mode,
            "batch_id": req.batch_id,
        }

    # Count-only preview mode (backward compatible) — no browser opened
    if req.mode == "count_only":
        return {
            "mode": "count_only",
            "message": "Count-only preview complete: no browser launched and no form submissions made.",
            "count_only": True,
            "would_submit": len(pending),
            "batch_id": req.batch_id,
        }

    # Browser validation (dry run) vs test vs production
    engine_mode = "dry_run" if req.mode in ("dry_run", "browser_validation", "browser_dry_run") else req.mode

    # Apply limit for test mode (or dry_run if explicitly limited)
    if engine_mode == "test":
        cap = req.limit or 5
        pending = pending[:cap]
    elif engine_mode == "dry_run" and req.limit:
        pending = pending[:req.limit]

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
                await db.flush()
        except Exception as e:
            logger.warning(f"Error syncing debug_meta project mappings in run_automation: {e}")

    # Bulk-fetch BOQ mappings
    mappings_res = await db.execute(select(WorkerMapping).where(WorkerMapping.session_id == session_id))
    boq_map = {m.worker_type: m for m in mappings_res.scalars().all()}

    # Build serialisable records list for the background task
    records_payload = []
    for rec in pending:
        worker = rec.worker
        mapping = boq_map.get(worker.worker_type) if worker else None
        
        # Select custom override if set, otherwise fallback to mapping default
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

    import subprocess
    import sys
    import os

    python_exe = sys.executable
    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "run_submission.py"))
    
    subprocess.Popen([
        python_exe,
        script_path,
        "--batch-id", req.batch_id,
        "--mode", engine_mode,
        "--session-id", session_id
    ])

    mode_label = "Browser validation (dry run)" if engine_mode == "dry_run" else f"Submission ({engine_mode})"
    return {
        "mode": engine_mode,
        "batch_id": req.batch_id,
        "queued": len(records_payload),
        "message": f"{len(records_payload)} records queued for {mode_label}. Poll /status/{req.batch_id} for live progress.",
    }


@router.post("/pause/{batch_id}")
async def pause_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    batch = await db.get(UploadBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")
    if batch.session_id != session_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    batch.status = "Paused"
    await db.commit()
    return {"batch_id": batch_id, "status": "Paused"}


@router.post("/cancel/{batch_id}")
async def cancel_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    batch = await db.get(UploadBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")
    if batch.session_id != session_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    batch.status = "Cancelled"
    await db.commit()
    return {"batch_id": batch_id, "status": "Cancelled"}


@router.get("/status/{batch_id}")
async def get_batch_status(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Poll the live submission progress of a batch."""
    batch_res = await db.execute(
        select(UploadBatch).where(UploadBatch.id == batch_id)
    )
    batch = batch_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")
    if batch.session_id != session_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    records_res = await db.execute(
        select(AttendanceRecord).where(AttendanceRecord.batch_id == batch_id)
    )
    records = records_res.scalars().all()
    total = len(records)
    n_submitted = sum(1 for r in records if r.status == "Submitted")
    n_failed = sum(1 for r in records if r.status == "Failed")
    n_needs_review = sum(1 for r in records if r.status in ("Needs Review", "Uncertain"))
    n_pending = sum(1 for r in records if r.status == "Pending")

    # Fetch submission results for live logs and dry-run stats
    rec_ids = [str(r.id) for r in records]
    all_results = []
    if rec_ids:
        results_res = await db.execute(
            select(SubmissionResult)
            .where(
                SubmissionResult.record_id.in_(rec_ids),
                SubmissionResult.session_id == session_id,
            )
            .order_by(SubmissionResult.timestamp.desc())
        )
        all_results = results_res.scalars().all()

    # Dry-run verified IDs
    dry_run_verified_ids = {
        r.record_id for r in all_results
        if r.status == "Success" and "[Dry Run]" in (r.error_message or "")
    }
    n_dry_run_verified = len(dry_run_verified_ids)

    # Calculate terminal progress percentage
    if batch.status in ("Completed", "Cancelled", "Failed"):
        if batch.status == "Completed":
            progress_pct = 100
        else:
            processed = n_submitted + n_failed + n_needs_review + (n_dry_run_verified if n_submitted == 0 else 0)
            progress_pct = min(100, round(processed / total * 100)) if total > 0 else 0
    else:
        processed = n_submitted + n_failed + n_needs_review + (n_dry_run_verified if n_submitted == 0 else 0)
        progress_pct = min(100, round(processed / total * 100)) if total > 0 else 0

    logs = []
    for r in reversed(all_results[:20]):
        if r.status == "Success":
            if "[Dry Run]" in (r.error_message or ""):
                logs.append(f"[DRY RUN]: ✓ {r.worker_name} @ {r.project_name} ({r.attendance_date}) - Verified")
            else:
                logs.append(f"[PLAYWRIGHT]: ✓ {r.worker_name} @ {r.project_name} ({r.attendance_date})")
        elif r.status in ("Needs Review", "Uncertain"):
            logs.append(f"[NEEDS REVIEW]: ⚠ {r.worker_name} @ {r.project_name} — {r.error_message or 'Review required'}")
        else:
            logs.append(f"[ERROR]: ✗ {r.worker_name} — {r.error_message or 'Unknown error'}")

    return {
        "batch_id": batch_id,
        "batch_status": batch.status,
        "total": total,
        "submitted": n_submitted,
        "failed": n_failed,
        "needs_review": n_needs_review,
        "uncertain": n_needs_review,
        "dry_run_verified": n_dry_run_verified,
        "pending": n_pending,
        "progress_pct": progress_pct,
        "logs": logs,
    }
