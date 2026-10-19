"""
GET  /api/records/preview/{batch_id}   — preview table data
GET  /api/records/batches              — list all batches for current session
POST /api/records/session/reset        — delete all records for current session
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.api.deps import get_db, get_session_id
from app.db.models import UploadBatch, AttendanceRecord, WorkerMapping
import json

router = APIRouter(prefix="/records", tags=["Records"])


@router.get("/preview/{batch_id}")
async def get_preview(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Return all attendance records for a batch, joined with BOQ mapping data."""
    # Verify batch session ownership
    batch_res = await db.execute(
        select(UploadBatch).where(UploadBatch.id == batch_id)
    )
    batch = batch_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")
    if batch.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not have access to this batch.",
        )

    # Fetch attendance records with worker eagerly loaded
    records_res = await db.execute(
        select(AttendanceRecord)
        .options(selectinload(AttendanceRecord.worker))
        .where(
            AttendanceRecord.batch_id == batch_id,
            AttendanceRecord.session_id == session_id
        )
        .order_by(AttendanceRecord.attendance_date)
    )
    att_records = records_res.scalars().all()

    # Bulk-fetch BOQ worker mappings (keyed by worker_type)
    mappings_res = await db.execute(
        select(WorkerMapping).where(WorkerMapping.session_id == session_id)
    )
    boq_map = {m.worker_type: m for m in mappings_res.scalars().all()}

    preview_records = []
    for idx, rec in enumerate(att_records):
        worker = rec.worker
        mapping = boq_map.get(worker.worker_type) if worker else None

        # Determine status label
        raw_status = (rec.status or "Pending").lower()
        if raw_status in ("submitted",):
            ui_status = "valid"
        elif raw_status in ("failed",):
            ui_status = "invalid"
        else:
            ui_status = "valid"  # Pending → valid (not yet submitted)

        # Determine Description (use record custom override if present, otherwise default to role mapping description)
        desc_val = rec.custom_description if rec.custom_description is not None else (mapping.description if mapping else "")

        preview_records.append({
            "id": rec.id,
            "attendance_date": rec.attendance_date.strftime("%d/%m") if rec.attendance_date else "",
            "worker_name": worker.name if worker else "Unknown",
            "worker_type": worker.worker_type if worker else "",
            "project_name": rec.project_name,
            "boq_category": mapping.boq_category if mapping else "",
            "description": desc_val,
            "duration": rec.duration,
            "status": ui_status,
            "error_message": None,
        })

    debug_meta = {}
    if batch.debug_meta:
        try:
            debug_meta = json.loads(batch.debug_meta)
        except Exception:
            pass

    return {
        "batch_id": batch_id,
        "form_url": batch.form_url,
        "batch_status": batch.status,
        "total": len(preview_records),
        "records": preview_records,
        "debug_meta": debug_meta
    }


@router.get("/batches")
async def list_batches(
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """List all upload batches for the current session, newest first."""
    result = await db.execute(
        select(UploadBatch)
        .where(UploadBatch.session_id == session_id)
        .order_by(UploadBatch.created_at.desc())
    )
    batches = result.scalars().all()
    return {
        "batches": [
            {
                "id": b.id,
                "status": b.status,
                "form_url": b.form_url,
                "created_at": b.created_at.isoformat() if b.created_at else None,
              }
            for b in batches
        ]
    }


@router.post("/session/reset")
async def reset_session(
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Purge all attendance records, submission results, mapping history, and upload batches for the current session."""
    import shutil
    import os
    from app.db.models import SubmissionResult, AttendanceRecord, UploadBatch, WorkerMapping, Worker, FormProfile

    # Delete related children and session records
    await db.execute(delete(SubmissionResult).where(SubmissionResult.session_id == session_id))
    await db.execute(delete(AttendanceRecord).where(AttendanceRecord.session_id == session_id))
    await db.execute(delete(UploadBatch).where(UploadBatch.session_id == session_id))
    await db.execute(delete(WorkerMapping).where(WorkerMapping.session_id == session_id))
    await db.execute(delete(Worker).where(Worker.session_id == session_id))
    await db.execute(delete(FormProfile).where(FormProfile.session_id == session_id))

    await db.commit()

    # Wipe Playwright session context directory
    session_dir = os.path.abspath(f"playwright_sessions/{session_id}")
    if os.path.exists(session_dir):
        import time
        import uuid
        import asyncio
        deleted = False
        for _ in range(5):
            try:
                shutil.rmtree(session_dir)
                deleted = True
                break
            except Exception:
                time.sleep(0.2)
        
        if not deleted:
            try:
                trash_dir = os.path.abspath(f"playwright_sessions/trash_{uuid.uuid4().hex}")
                os.rename(session_dir, trash_dir)
                def delete_trash():
                    try:
                        shutil.rmtree(trash_dir)
                    except Exception:
                        pass
                import asyncio
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, delete_trash)
            except Exception:
                pass

    return {"message": "Session wiped successfully."}


from pydantic import BaseModel
from typing import Optional

class UpdateRecordRequest(BaseModel):
    project_name: str
    worker_name: str
    description: Optional[str] = None
    duration: Optional[str] = None

class ProjectAliasRequest(BaseModel):
    batch_id: str
    input_project: str
    resolved_project: str

class WorkerAliasRequest(BaseModel):
    batch_id: str
    input_worker: str
    resolved_worker: str

class RefreshCatalogRequest(BaseModel):
    form_url: str


@router.put("/attendance/{record_id}")
async def update_attendance_record(
    record_id: str,
    req: UpdateRecordRequest,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Updates a single attendance record's project, worker name, custom description, and duration."""
    res = await db.execute(
        select(AttendanceRecord)
        .options(selectinload(AttendanceRecord.worker))
        .where(AttendanceRecord.id == record_id, AttendanceRecord.session_id == session_id)
    )
    rec = res.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found.")

    rec.project_name = req.project_name
    if rec.worker:
        rec.worker.name = req.worker_name
        
    # Store description directly on this specific record
    rec.custom_description = req.description

    if req.duration is not None:
        rec.duration = req.duration

    await db.commit()
    return {"status": "success", "message": "Record updated successfully."}


@router.post("/project-alias")
async def save_project_alias(
    req: ProjectAliasRequest,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Saves a global project alias mapping and bulk-updates all records in the batch with matching input project name."""
    from app.db.models import ProjectAlias
    from sqlalchemy import update, func, or_
    
    clean_input = req.input_project.strip()
    clean_resolved = req.resolved_project.strip()

    # 1. Upsert global alias dictionary (case-insensitive search)
    res_alias = await db.execute(
        select(ProjectAlias).where(func.lower(ProjectAlias.input_project) == clean_input.lower())
    )
    alias = res_alias.scalar_one_or_none()
    if not alias:
        alias = ProjectAlias(
            input_project=clean_input,
            resolved_project=clean_resolved
        )
        db.add(alias)
    else:
        alias.input_project = clean_input
        alias.resolved_project = clean_resolved

    # 2. Get batch to read debug_meta and find previous resolved project name
    batch_res = await db.execute(
        select(UploadBatch).where(UploadBatch.id == req.batch_id, UploadBatch.session_id == session_id)
    )
    batch = batch_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")

    prev_resolved = None
    names_to_match = {clean_input}
    if batch.debug_meta:
        try:
            debug_data = json.loads(batch.debug_meta)
            resolutions = debug_data.get("project_resolution", [])
            found = False
            for r in resolutions:
                if (r.get("input_project") or "").strip().lower() == clean_input.lower():
                    prev_resolved = r.get("resolved_project")
                    r["resolved_project"] = clean_resolved
                    r["confidence"] = 100
                    r["status"] = "Auto-Accepted"
                    r["match_type"] = "Manual Correction"
                    found = True
            if not found:
                resolutions.append({
                    "input_project": clean_input,
                    "resolved_project": clean_resolved,
                    "confidence": 100,
                    "status": "Auto-Accepted",
                    "match_type": "Manual Correction"
                })
            debug_data["project_resolution"] = resolutions
            batch.debug_meta = json.dumps(debug_data)
        except Exception:
            pass

    if prev_resolved and prev_resolved.strip():
        names_to_match.add(prev_resolved.strip())

    # 3. Bulk update all records in the batch having matching project_name (case-insensitive)
    conditions = [func.lower(AttendanceRecord.project_name) == n.lower() for n in names_to_match if n]
    if conditions:
        stmt = (
            update(AttendanceRecord)
            .where(
                AttendanceRecord.batch_id == req.batch_id,
                AttendanceRecord.session_id == session_id,
                or_(*conditions)
            )
            .values(project_name=clean_resolved)
        )
        await db.execute(stmt)
    await db.commit()

    return {"status": "success", "message": f"Global alias saved. Bulk updated batch records to {clean_resolved}."}


@router.post("/worker-alias")
async def save_worker_alias(
    req: WorkerAliasRequest,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Saves a global worker alias mapping and bulk-updates all records in the batch with matching input worker name."""
    from app.db.models import WorkerAlias, Worker
    
    # 1. Upsert global alias dictionary
    res_alias = await db.execute(select(WorkerAlias).where(WorkerAlias.input_worker == req.input_worker))
    alias = res_alias.scalar_one_or_none()
    if not alias:
        alias = WorkerAlias(
            input_worker=req.input_worker,
            resolved_worker=req.resolved_worker
        )
        db.add(alias)
    else:
        alias.resolved_worker = req.resolved_worker

    # 2. Get batch to read debug_meta and find previous resolved worker name
    batch_res = await db.execute(select(UploadBatch).where(UploadBatch.id == req.batch_id, UploadBatch.session_id == session_id))
    batch = batch_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")

    prev_resolved = req.input_worker
    resolutions = []
    if batch.debug_meta:
        try:
            debug_data = json.loads(batch.debug_meta)
            resolutions = debug_data.get("worker_resolution", [])
            for r in resolutions:
                if r.get("input_worker") == req.input_worker:
                    prev_resolved = r.get("resolved_worker")
                    r["resolved_worker"] = req.resolved_worker
                    r["confidence"] = 100
                    r["status"] = "Auto-Accepted"
                    r["match_type"] = "Manual Correction"
            batch.debug_meta = json.dumps(debug_data)
        except Exception:
            pass

    # 3. Bulk update all workers in this session having either input_worker or prev_resolved as name
    # First, find all workers that need updating
    from sqlalchemy import update
    workers_stmt = select(Worker).where(
        Worker.session_id == session_id,
        Worker.name.in_([req.input_worker, prev_resolved])
    )
    res_workers = await db.execute(workers_stmt)
    workers_to_update = res_workers.scalars().all()
    for worker in workers_to_update:
        worker.name = req.resolved_worker
        # Try inferring worker type from new resolved name
        if "_" in req.resolved_worker:
            worker.worker_type = req.resolved_worker.split("_")[0]
        elif " " in req.resolved_worker:
            # e.g., CARPENTER NARESH -> CARPENTER
            worker.worker_type = req.resolved_worker.split(" ")[0]

    await db.commit()

    return {"status": "success", "message": f"Global worker alias saved. Bulk updated batch workers to {req.resolved_worker}."}


@router.get("/project-catalog/{batch_id}")
async def get_batch_project_catalog(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Fetches cached or scraped project names catalog associated with the batch's form URL."""
    batch_res = await db.execute(select(UploadBatch).where(UploadBatch.id == batch_id, UploadBatch.session_id == session_id))
    batch = batch_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")

    from app.services.project_resolver import ProjectResolver
    resolver = ProjectResolver(db, session_id)
    catalog = await resolver.get_project_catalog(batch.form_url)
    return {"catalog": catalog}


@router.get("/worker-catalog/{batch_id}")
async def get_batch_worker_catalog(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Fetches cached or scraped worker names catalog associated with the batch's form URL."""
    batch_res = await db.execute(select(UploadBatch).where(UploadBatch.id == batch_id, UploadBatch.session_id == session_id))
    batch = batch_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")

    from app.services.worker_resolver import WorkerResolver
    resolver = WorkerResolver(db, session_id)
    catalog = await resolver.get_worker_catalog(batch.form_url)
    return {"catalog": catalog}


@router.delete("/batches/{batch_id}")
async def delete_upload_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Deletes a specific upload batch and its associated attendance records and submission results."""
    from app.db.models import SubmissionResult, AttendanceRecord, UploadBatch
    
    # 1. Fetch batch to verify ownership
    batch_res = await db.execute(
        select(UploadBatch).where(UploadBatch.id == batch_id, UploadBatch.session_id == session_id)
    )
    batch = batch_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")
        
    # 2. Delete related submission results and records
    records_res = await db.execute(
        select(AttendanceRecord.id).where(AttendanceRecord.batch_id == batch_id)
    )
    record_ids = [str(rid) for rid in records_res.scalars().all()]
    
    if record_ids:
        await db.execute(
            delete(SubmissionResult).where(SubmissionResult.record_id.in_(record_ids))
        )
        await db.execute(
            delete(AttendanceRecord).where(AttendanceRecord.id.in_(record_ids))
        )
        
    # 3. Delete the batch itself
    await db.execute(
        delete(UploadBatch).where(UploadBatch.id == batch_id)
    )
    
    await db.commit()
    return {"status": "success", "message": f"Batch {batch_id} deleted successfully."}


@router.post("/project-catalog/refresh")
async def refresh_project_catalog(
    req: RefreshCatalogRequest,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Forces Playwright scrape to update the cached project catalog for the given Google Form URL."""
    from app.services.project_resolver import ProjectResolver
    resolver = ProjectResolver(db, session_id)
    catalog = await resolver.get_project_catalog(req.form_url, force_refresh=True)
    return {"catalog": catalog, "status": "success"}


@router.post("/worker-catalog/refresh")
async def refresh_worker_catalog(
    req: RefreshCatalogRequest,
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
):
    """Forces Playwright scrape to update the cached worker catalog for the given Google Form URL."""
    from app.services.worker_resolver import WorkerResolver
    resolver = WorkerResolver(db, session_id)
    catalog = await resolver.get_worker_catalog(req.form_url, force_refresh=True)
    return {"catalog": catalog, "status": "success"}



