import pytest
import pytest_asyncio
from app.services.project_resolver import normalize_name, get_tokens, ProjectResolver
from app.db.models import Base, ProjectAlias
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

TEST_DATABASE_URL = "sqlite+aiosqlite:///./test_attendance.db"

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)

TestAsyncSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def db_session(setup_db):
    async with TestAsyncSessionLocal() as session:
        yield session

def test_normalize_name():
    assert normalize_name("SYCON 61") == "sycon61"
    assert normalize_name("SYCON_C61") == "syconc61"
    assert normalize_name("sycon-61") == "sycon61"
    assert normalize_name("sycon_61") == "sycon61"

def test_get_tokens():
    assert get_tokens("CENTREO CG-04") == ["centreo", "cg", "04"]
    assert get_tokens("SYCON_C61") == ["sycon", "c61"]

@pytest.mark.asyncio
async def test_resolver_matching_pipeline(db_session):
    # Mocking resolver with database connection
    resolver = ProjectResolver(db_session, "test_session")
    
    # Let's mock a catalog
    catalog = [
        "SYCON_C61",
        "CONCORDE ABODE",
        "BRIGADE COSMOPOLIS",
        "SALAPURIA MAGNIFICIA C-702",
        "CENTREO CG-04"
    ]
    
    # We will temporarily override get_project_catalog to return our mock catalog
    async def mock_catalog(*args, **kwargs):
        return catalog
    resolver.get_project_catalog = mock_catalog
    
    raw_records = [
        {"project_name": "SYCON 61"},
        {"project_name": "CONCORD"},
        {"project_name": "COSMOPOLIS"},
        {"project_name": "SALARPURIYA 702"},
        {"project_name": "CENTREO 04"}
    ]
    
    res = await resolver.resolve_projects(raw_records, "http://mockform.com")
    resolutions = {r["input_project"]: r for r in res["resolutions"]}
    
    assert resolutions["SYCON 61"]["resolved_project"] == "SYCON_C61"
    assert resolutions["CONCORD"]["resolved_project"] == "CONCORDE ABODE"
    assert resolutions["COSMOPOLIS"]["resolved_project"] == "BRIGADE COSMOPOLIS"
    assert resolutions["SALARPURIYA 702"]["resolved_project"] == "SALAPURIA MAGNIFICIA C-702"
    assert resolutions["CENTREO 04"]["resolved_project"] == "CENTREO CG-04"
    
    assert resolutions["SYCON 61"]["confidence"] >= 90
    assert resolutions["CONCORD"]["confidence"] >= 80


@pytest.mark.asyncio
async def test_ambiguous_project_mapping_requires_user_selection(db_session):
    """Verify that ambiguous mappings like 'Sobha Royal Pavilion' never guess between Parisha and Sitara."""
    resolver = ProjectResolver(db_session, "test_session_ambig")
    
    catalog = [
        "Sobha Royal Pavilion - Parisha",
        "Sobha Royal Pavilion - Sitara",
        "Brigade Cosmopolis"
    ]
    
    async def mock_catalog(*args, **kwargs):
        return catalog
    resolver.get_project_catalog = mock_catalog
    
    raw_records = [
        {"project_name": "SOBHA ROYAL PAVILION"}
    ]
    
    res = await resolver.resolve_projects(raw_records, "http://mockform.com")
    resolutions = {r["input_project"]: r for r in res["resolutions"]}
    
    ambig_res = resolutions["SOBHA ROYAL PAVILION"]
    assert ambig_res["status"] == "Needs Review"
    assert ambig_res["confidence"] == 0
    assert ambig_res["match_type"] == "Ambiguous Match (Manual Selection Required)"
    assert ambig_res["resolved_project"] == ""
    
    # Crucial: verify it did NOT guess either Parisha or Sitara for raw record
    assert res["resolved_records"][0]["project_name"] == "SOBHA ROYAL PAVILION"


@pytest.mark.asyncio
async def test_change_mapping_and_save_alias(db_session):
    """Test changing a mapping, saving it to DB and ProjectAlias, and retrieving it via preview API."""
    import uuid
    import datetime
    import json
    from sqlalchemy import select
    from app.db.models import UploadBatch, AttendanceRecord, Worker, ProjectAlias
    from app.api.records import save_project_alias, get_preview, ProjectAliasRequest
    
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    batch_id = str(uuid.uuid4())
    
    batch = UploadBatch(
        id=batch_id,
        session_id=session_id,
        form_url="http://mockform.com",
        status="Pending",
        debug_meta=json.dumps({
            "project_resolution": [
                {
                    "input_project": "SOBHA ROYAL PAVILION",
                    "resolved_project": "",
                    "confidence": 0,
                    "status": "Needs Review",
                    "match_type": "Ambiguous Match (Manual Selection Required)"
                }
            ]
        })
    )
    db_session.add(batch)
    
    worker = Worker(
        id=str(uuid.uuid4()),
        session_id=session_id,
        name="Ramesh Carpenter",
        worker_type="Carpenter"
    )
    db_session.add(worker)
    await db_session.flush()
    
    record = AttendanceRecord(
        id=str(uuid.uuid4()),
        session_id=session_id,
        batch_id=batch_id,
        worker_id=worker.id,
        project_name="SOBHA ROYAL PAVILION",
        attendance_date=datetime.date(2026, 10, 15),
        duration="8-10 Hours",
        status="Pending"
    )
    db_session.add(record)
    await db_session.commit()
    
    # 1. User selects canonical option from dropdown
    req = ProjectAliasRequest(
        batch_id=batch_id,
        input_project="SOBHA ROYAL PAVILION",
        resolved_project="Sobha Royal Pavilion - Parisha"
    )
    res = await save_project_alias(req, db_session, session_id)
    assert res["status"] == "success"
    
    # 2. Verify AttendanceRecord in DB was updated to canonical name
    rec_check = await db_session.execute(select(AttendanceRecord).where(AttendanceRecord.id == record.id))
    saved_rec = rec_check.scalar_one()
    assert saved_rec.project_name == "Sobha Royal Pavilion - Parisha"
    
    # 3. Verify ProjectAlias was saved for future uploads
    alias_check = await db_session.execute(
        select(ProjectAlias).where(ProjectAlias.input_project == "SOBHA ROYAL PAVILION")
    )
    saved_alias = alias_check.scalar_one()
    assert saved_alias.resolved_project == "Sobha Royal Pavilion - Parisha"
    
    # 4. Verify Preview API returns the updated canonical project name and debug_meta
    preview = await get_preview(batch_id, db_session, session_id)
    assert len(preview["records"]) == 1
    assert preview["records"][0]["project_name"] == "Sobha Royal Pavilion - Parisha"
    res_list = preview["debug_meta"]["project_resolution"]
    assert res_list[0]["resolved_project"] == "Sobha Royal Pavilion - Parisha"
    assert res_list[0]["confidence"] == 100
    assert res_list[0]["status"] == "Auto-Accepted"


@pytest.mark.asyncio
async def test_submission_payload_uses_canonical_project_name(db_session):
    """Test that automation submission engine payload receives the exact canonical project name."""
    import uuid
    import datetime
    import json
    from unittest.mock import patch, MagicMock
    from fastapi import BackgroundTasks
    from sqlalchemy import select
    from app.db.models import UploadBatch, AttendanceRecord, Worker
    from app.api.automation import RunRequest, run_automation
    
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    batch_id = str(uuid.uuid4())
    
    batch = UploadBatch(
        id=batch_id,
        session_id=session_id,
        form_url="https://docs.google.com/forms/d/e/1FAIpQLSc_test/viewform",
        status="Pending",
        debug_meta=json.dumps({
            "project_resolution": [
                {
                    "input_project": "SOBHA ROYAL PAVILION",
                    "resolved_project": "Sobha Royal Pavilion - Parisha",
                    "confidence": 100,
                    "status": "Auto-Accepted",
                    "match_type": "Manual Correction"
                }
            ]
        })
    )
    db_session.add(batch)
    
    worker = Worker(
        id=str(uuid.uuid4()),
        session_id=session_id,
        name="Ramesh Carpenter",
        worker_type="Carpenter"
    )
    db_session.add(worker)
    await db_session.flush()
    
    # 1. Setup batch where raw record has "SOBHA ROYAL PAVILION"
    # but debug_meta has resolved it to canonical "Sobha Royal Pavilion - Parisha"
    record = AttendanceRecord(
        id=str(uuid.uuid4()),
        session_id=session_id,
        batch_id=batch_id,
        worker_id=worker.id,
        project_name="SOBHA ROYAL PAVILION",
        attendance_date=datetime.date(2026, 10, 15),
        duration="8-10 Hours",
        status="Pending"
    )
    db_session.add(record)
    await db_session.commit()
    
    with patch("app.api.automation.check_google_session_status", return_value={"connected": True}), \
         patch("subprocess.Popen") as mock_popen:
        
        req = RunRequest(batch_id=batch_id, mode="test", limit=5)
        bg = BackgroundTasks()
        res = await run_automation(req, bg, db_session, session_id)
        
        assert res["batch_id"] == batch_id
        assert res["queued"] == 1
        assert mock_popen.called
        
        # Verify run_automation synchronized record.project_name to canonical value before queuing
        rec_check = await db_session.execute(select(AttendanceRecord).where(AttendanceRecord.id == record.id))
        persisted_rec = rec_check.scalar_one()
        assert persisted_rec.project_name == "Sobha Royal Pavilion - Parisha"


@pytest.mark.asyncio
async def test_alias_history_does_not_override_newer_explicit_selection(db_session):
    """Verify that an older alias does NOT override a newer explicit selection for the current record."""
    import uuid
    import datetime
    import json
    from app.db.models import UploadBatch, AttendanceRecord, Worker, ProjectAlias
    from app.api.records import save_project_alias, get_preview, ProjectAliasRequest
    
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    batch_id = str(uuid.uuid4())
    test_input = "SOBHA ROYAL PAVILION AMBIG"
    
    # Prior alias in DB: mapped to Parisha
    old_alias = ProjectAlias(
        input_project=test_input,
        resolved_project="Sobha Royal Pavilion - Parisha"
    )
    db_session.add(old_alias)
    
    batch = UploadBatch(
        id=batch_id,
        session_id=session_id,
        form_url="http://mockform.com",
        status="Pending",
        debug_meta=json.dumps({
            "project_resolution": [
                {
                    "input_project": test_input,
                    "resolved_project": "Sobha Royal Pavilion - Parisha",
                    "confidence": 100,
                    "status": "Auto-Accepted",
                    "match_type": "Learned Alias"
                }
            ]
        })
    )
    db_session.add(batch)
    
    worker = Worker(
        id=str(uuid.uuid4()),
        session_id=session_id,
        name="Suresh Welder",
        worker_type="Welder"
    )
    db_session.add(worker)
    await db_session.flush()
    
    record = AttendanceRecord(
        id=str(uuid.uuid4()),
        session_id=session_id,
        batch_id=batch_id,
        worker_id=worker.id,
        project_name="Sobha Royal Pavilion - Parisha",
        attendance_date=datetime.date(2026, 10, 15),
        duration="8-10 Hours",
        status="Pending"
    )
    db_session.add(record)
    await db_session.commit()
    
    # User now explicitly selects "Sobha Royal Pavilion - Sitara" for this batch
    req = ProjectAliasRequest(
        batch_id=batch_id,
        input_project=test_input,
        resolved_project="Sobha Royal Pavilion - Sitara"
    )
    await save_project_alias(req, db_session, session_id)
    
    # Preview must return the NEW canonical name Sitara, NOT Parisha
    preview = await get_preview(batch_id, db_session, session_id)
    assert preview["records"][0]["project_name"] == "Sobha Royal Pavilion - Sitara"
    assert preview["debug_meta"]["project_resolution"][0]["resolved_project"] == "Sobha Royal Pavilion - Sitara"

