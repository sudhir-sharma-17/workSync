import pytest
from unittest.mock import AsyncMock, MagicMock
from app.automation.submission_engine import (
    PlaywrightSubmissionEngine,
    REQUIRED_SYSTEM_FIELDS,
)
from app.db.models import AttendanceRecord, SubmissionResult, UploadBatch


VALID_RECORD = {
    "id": "rec-test-001",
    "attendance_date": "15/04",
    "worker_name": "CARPENTER NARESH",
    "project_name": "VILLA 101",
    "boq_category": "CARPENTRY",
    "duration": "8.0",
    "description": "Door framing works",
}

VALID_FIELD_MAP = {
    "attendance_date": {"label": "Attendance Date", "type": "text"},
    "worker_name": {"label": "Name of Labour", "type": "listbox"},
    "project_name": {"label": "Project Name", "type": "listbox"},
    "boq_category": {"label": "BOQ Category", "type": "listbox"},
    "duration": {"label": "Duration (Hours)", "type": "text"},
    "description": {"label": "Work Description", "type": "textarea"},
}


class MockElement:
    def __init__(self, text="", value="", aria_label="", aria_selected=False, is_visible=True):
        self._text = text
        self._value = value
        self._aria_label = aria_label
        self._aria_selected = aria_selected
        self._is_visible = is_visible

    async def inner_text(self, *args, **kwargs):
        return self._text

    async def text_content(self, *args, **kwargs):
        return self._text

    async def input_value(self):
        return self._value

    async def fill(self, val):
        self._value = str(val)

    async def blur(self):
        pass

    async def click(self):
        self._aria_selected = True

    async def get_attribute(self, attr):
        if attr == "aria-label":
            return self._aria_label
        if attr == "aria-selected":
            return "true" if self._aria_selected else "false"
        if attr == "aria-disabled":
            return "false"
        return None

    async def is_visible(self):
        return self._is_visible

    async def count(self):
        return 1

    async def wait_for(self, **kwargs):
        pass


class MockLocator:
    def __init__(self, elements=None):
        self._elements = elements or []

    @property
    def first(self):
        return self._elements[0] if self._elements else MockElement()

    @property
    def last(self):
        return self._elements[-1] if self._elements else MockElement()

    def filter(self, has_text=""):
        filtered = [el for el in self._elements if has_text.lower() in getattr(el, "_label", "").lower()]
        return MockLocator(filtered if filtered else self._elements)

    async def all(self):
        return self._elements

    async def count(self):
        return len(self._elements)

    async def click(self):
        if self._elements:
            await self._elements[0].click()

    async def fill(self, val):
        if self._elements:
            await self._elements[0].fill(val)

    async def blur(self):
        if self._elements:
            await self._elements[0].blur()

    async def input_value(self):
        if self._elements:
            return await self._elements[0].input_value()
        return ""

    async def inner_text(self, *args, **kwargs):
        if self._elements:
            return await self._elements[0].inner_text(*args, **kwargs)
        return ""

    async def text_content(self, *args, **kwargs):
        if self._elements:
            return await self._elements[0].text_content(*args, **kwargs)
        return ""

    async def get_attribute(self, attr):
        if self._elements:
            return await self._elements[0].get_attribute(attr)
        return None

    async def wait_for(self, **kwargs):
        pass


class MockQuestionContainer:
    def __init__(self, sys_field, field_info, page_ref):
        self.sys_field = sys_field
        self.field_info = field_info
        self.page_ref = page_ref
        self._label = field_info["label"]
        self.input_type = field_info["type"]

        # Date widget multiple inputs (Day, Month)
        if sys_field == "attendance_date":
            self.day_input = MockElement(aria_label="Day")
            self.month_input = MockElement(aria_label="Month")
            self.text_inputs = [self.day_input, self.month_input]
        else:
            self.main_input = MockElement()
            self.text_inputs = [self.main_input]

        # Dropdown
        self.dropdown_btn = MockElement(text="Choose")

    async def wait_for(self, **kwargs):
        pass

    async def inner_text(self):
        if self.input_type == "listbox":
            return f"{self._label} {await self.dropdown_btn.inner_text()}"
        return self._label

    def locator(self, selector):
        if selector == 'div[role="listbox"]':
            return MockLocator([self.dropdown_btn])
        if 'input[type="text"]' in selector or 'textarea' in selector:
            return MockLocator(self.text_inputs)
        return MockLocator([self.main_input])


class MockPlaywrightPage:
    def __init__(self, dropdown_options=None, submission_behavior="success"):
        self.submission_behavior = submission_behavior  # "success", "timeout", "rejection", "error_after_submit"
        self.dropdown_options = dropdown_options or {
            "worker_name": ["CARPENTER NARESH", "PAINTER RAMESH"],
            "project_name": ["VILLA 101", "TOWER B"],
            "boq_category": ["CARPENTRY", "MASONRY"],
        }
        self.submit_button = MockElement(text="Submit")
        self.submit_clicked = False
        self._closed = False
        self.url = "https://docs.google.com/forms/d/e/test-form/viewform"
        self.containers = {}

        # Set up containers for all fields in VALID_FIELD_MAP
        for sf, info in VALID_FIELD_MAP.items():
            self.containers[sf] = MockQuestionContainer(sf, info, self)

        # Build options for page-level dropdown query
        self.current_open_field = None

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True

    async def goto(self, url, **kwargs):
        self.url = url

    async def wait_for_timeout(self, ms):
        pass

    async def wait_for_selector(self, selector, **kwargs):
        pass

    async def screenshot(self, **kwargs):
        pass

    async def content(self):
        if self.submission_behavior == "success" and self.submit_clicked:
            return "<html><body>Your response has been recorded</body></html>"
        return "<html><body>Google Form content</body></html>"

    def locator(self, selector):
        if 'Submit another' in selector:
            btn = MockElement(text="Submit another response")
            async def raise_no_another(**kwargs):
                raise Exception("Not visible")
            btn.wait_for = raise_no_another
            btn.click = raise_no_another
            return MockLocator([btn])

        if 'div[role="button"]:has-text("Submit")' in selector or 'button:has-text("Submit")' in selector:
            async def track_submit_click():
                self.submit_clicked = True
            self.submit_button.click = track_submit_click
            return MockLocator([self.submit_button])

        if 'div[role="option"]' in selector:
            if '[aria-selected="true"]' in selector:
                # Return selected options
                selected = []
                for sf, opts in self.dropdown_options.items():
                    c = self.containers.get(sf)
                    if c:
                        curr_txt = c.dropdown_btn._text
                        if curr_txt in opts:
                            selected.append(MockElement(text=curr_txt, aria_selected=True))
                return MockLocator(selected)
            else:
                # Return all options for current active dropdown
                opts = []
                for sf, opt_list in self.dropdown_options.items():
                    for name in opt_list:
                        opt_el = MockElement(text=name)
                        # When clicked, update the container's dropdown text
                        def make_click_fn(field=sf, text=name):
                            async def on_opt_click():
                                if field in self.containers:
                                    self.containers[field].dropdown_btn._text = text
                            return on_opt_click
                        opt_el.click = make_click_fn(sf, name)
                        opts.append(opt_el)
                return MockLocator(opts)

        if 'div[role="listitem"]' in selector or '.Qr7Oae' in selector:
            return MockLocator(list(self.containers.values()))

        if 'Submit another' in selector:
            btn = MockElement(text="Submit another response")
            async def raise_no_another():
                raise Exception("Not visible")
            btn.wait_for = raise_no_another
            return MockLocator([btn])

        return MockLocator([MockElement()])

    async def query_selector(self, selector):
        if self.submission_behavior == "rejection" and ('.RxsGPe' in selector or 'role="alert"' in selector or '.RHiWt' in selector):
            el = MockElement(text="This is a required question")
            return el
        return None

    async def query_selector_all(self, selector):
        el = await self.query_selector(selector)
        return [el] if el else []

    async def wait_for_function(self, fn_str, timeout=10000):
        if self.submission_behavior == "success":
            self.url = "https://docs.google.com/forms/u/0/d/e/test/formResponse"
            return True
        elif self.submission_behavior in ("timeout", "rejection", "error_after_submit"):
            raise Exception("Timeout waiting for submission confirmation")
        return True


def create_mock_engine(mode="production"):
    db_mock = AsyncMock()
    # Mocking AttendanceRecord lookup
    att_rec = AttendanceRecord(id="rec-test-001", status="Pending")
    db_mock.get = AsyncMock(return_value=att_rec)
    db_mock.commit = AsyncMock()
    db_mock.add = MagicMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value.all.return_value = []
    db_mock.execute = AsyncMock(return_value=mock_result)

    engine = PlaywrightSubmissionEngine(
        db_session=db_mock,
        batch_id="batch-001",
        session_id="session-001",
        mode=mode,
    )
    return engine, db_mock, att_rec


# ==============================================================================
# TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_validate_required_values_blocks_submission():
    """
    Requirement 3: Validate required values.
    Do not silently skip missing required values. Report which field is missing and block submission.
    """
    engine, db_mock, att_rec = create_mock_engine()
    page = MockPlaywrightPage()
    current_page_holder = [page]
    context_mock = AsyncMock()

    # Record missing duration and boq_category
    bad_record = dict(VALID_RECORD)
    bad_record["duration"] = ""
    bad_record["boq_category"] = None

    await engine._process_single_record(
        context_mock,
        bad_record,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    # 1. Submit was NEVER clicked
    assert page.submit_clicked is False

    # 2. Result was logged as Failed
    db_mock.add.assert_called_once()
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Failed"
    assert "boq_category" in logged_res.error_message
    assert "duration" in logged_res.error_message
    assert "Missing required value" in logged_res.error_message


@pytest.mark.asyncio
async def test_fail_safely_on_field_fill_error():
    """
    Requirement 1: Fail safely on field errors.
    If any required field fails to fill, raise an error and do not click Submit.
    """
    engine, db_mock, att_rec = create_mock_engine()
    page = MockPlaywrightPage()
    current_page_holder = [page]
    context_mock = AsyncMock()

    # Force a fill failure on duration container
    async def failing_fill(val):
        raise RuntimeError("Widget unresponsive")
    page.containers["duration"].main_input.fill = failing_fill

    await engine._process_single_record(
        context_mock,
        VALID_RECORD,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    # 1. Submit was NOT clicked
    assert page.submit_clicked is False

    # 2. Record logged as Failed
    db_mock.add.assert_called_once()
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Failed"
    assert "duration" in logged_res.error_message
    assert "Widget unresponsive" in logged_res.error_message


@pytest.mark.asyncio
async def test_exact_dropdown_matching_success():
    """
    Requirement 2: Exact dropdown matching.
    Exact match (and case-insensitive exact match) should succeed.
    """
    engine, db_mock, att_rec = create_mock_engine()
    page = MockPlaywrightPage()
    current_page_holder = [page]
    context_mock = AsyncMock()

    # Worker name matches exactly case-insensitively
    rec = dict(VALID_RECORD)
    rec["worker_name"] = "carpenter naresh"

    await engine._process_single_record(
        context_mock,
        rec,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    assert page.submit_clicked is True
    db_mock.add.assert_called_once()
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Success"


@pytest.mark.asyncio
async def test_exact_dropdown_matching_blocks_fuzzy_match():
    """
    Requirement 2: Exact dropdown matching.
    Automatic fuzzy selection for worker names and project names is REMOVED.
    If an exact option cannot be found, fail that record rather than selecting
    a potentially incorrect person or project.
    """
    engine, db_mock, att_rec = create_mock_engine()
    # Options available: CARPENTER NARESH, PAINTER RAMESH
    page = MockPlaywrightPage(dropdown_options={
        "worker_name": ["CARPENTER NARESH", "PAINTER RAMESH"],
        "project_name": ["VILLA 101"],
        "boq_category": ["CARPENTRY"],
    })
    current_page_holder = [page]
    context_mock = AsyncMock()

    # Input worker "NARESH" was fuzzy-matched to "CARPENTER NARESH" in old code.
    # Now it MUST NOT fuzzy match!
    fuzzy_record = dict(VALID_RECORD)
    fuzzy_record["worker_name"] = "NARESH"

    await engine._process_single_record(
        context_mock,
        fuzzy_record,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    # 1. Submit was NOT clicked
    assert page.submit_clicked is False

    # 2. Record logged as Failed due to exact option not found
    db_mock.add.assert_called_once()
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Failed"
    assert "Exact option 'NARESH' not found" in logged_res.error_message
    assert "fuzzy selection is disabled" in logged_res.error_message.lower()


@pytest.mark.asyncio
async def test_exact_dropdown_matching_blocks_fuzzy_project():
    """
    Requirement 2: Exact dropdown matching for project names as well.
    """
    engine, db_mock, att_rec = create_mock_engine()
    page = MockPlaywrightPage(dropdown_options={
        "worker_name": ["CARPENTER NARESH"],
        "project_name": ["VILLA 101", "VILLA 102"],
        "boq_category": ["CARPENTRY"],
    })
    current_page_holder = [page]
    context_mock = AsyncMock()

    # "VILLA 10" is close to "VILLA 101" - should NOT fuzzy match
    rec = dict(VALID_RECORD)
    rec["project_name"] = "VILLA 10"

    await engine._process_single_record(
        context_mock,
        rec,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    assert page.submit_clicked is False
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Failed"
    assert "Exact option 'VILLA 10' not found" in logged_res.error_message


@pytest.mark.asyncio
async def test_verify_fields_before_submission_blocks_mismatch():
    """
    Requirement 4: Verify fields before submission.
    Confirm that each of the six fields has been filled with the approved preview value.
    If a field does not match the approved value, block submission.
    """
    engine, db_mock, att_rec = create_mock_engine()
    page = MockPlaywrightPage()
    current_page_holder = [page]
    context_mock = AsyncMock()

    # Simulate container having wrong value during verification
    # e.g. duration input value returns "4.0" instead of approved "8.0"
    orig_fill = page.containers["duration"].main_input.fill
    async def wrong_fill(val):
        await orig_fill(val)
        page.containers["duration"].main_input._value = "4.0"
    page.containers["duration"].main_input.fill = wrong_fill

    await engine._process_single_record(
        context_mock,
        VALID_RECORD,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    # 1. Submit was NOT clicked
    assert page.submit_clicked is False

    # 2. Record logged as Failed due to verification failure
    db_mock.add.assert_called_once()
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Failed"
    assert "Pre-submission verification failed" in logged_res.error_message
    assert "duration" in logged_res.error_message


@pytest.mark.asyncio
async def test_dry_run_safe_behavior():
    """
    Requirement 6: Make dry-run safe.
    It must exercise field filling and validation without clicking Submit
    or creating actual Google Form responses.
    """
    engine, db_mock, att_rec = create_mock_engine(mode="dry_run")
    page = MockPlaywrightPage()
    current_page_holder = [page]
    context_mock = AsyncMock()

    await engine._process_single_record(
        context_mock,
        VALID_RECORD,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    # 1. Submit button was NOT clicked
    assert page.submit_clicked is False

    # 2. Fields WERE filled
    assert page.containers["attendance_date"].day_input._value == "15"
    assert page.containers["attendance_date"].month_input._value == "04"
    assert page.containers["duration"].main_input._value == "8.0"
    assert page.containers["description"].main_input._value == "Door framing works"
    assert page.containers["worker_name"].dropdown_btn._text == "CARPENTER NARESH"

    # 3. AttendanceRecord status is NOT "Submitted" (remains "Pending" for real submission later)
    assert att_rec.status == "Pending"

    # 4. Submission result logged with dry-run note
    db_mock.add.assert_called_once()
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Success"
    assert "[Dry Run]" in logged_res.error_message


@pytest.mark.asyncio
async def test_prevent_duplicate_submissions_on_uncertain_outcome():
    """
    Requirement 5: Prevent duplicate submissions.
    Do not blindly retry after clicking Submit if the outcome is uncertain.
    Check whether Google accepted the response first; if the result cannot be
    determined, mark it as uncertain and require manual review.
    """
    engine, db_mock, att_rec = create_mock_engine(mode="production")
    # Simulate a timeout waiting for confirmation
    page = MockPlaywrightPage(submission_behavior="timeout")
    current_page_holder = [page]
    context_mock = AsyncMock()

    await engine._process_single_record(
        context_mock,
        VALID_RECORD,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    # 1. Submit was clicked ONCE
    assert page.submit_clicked is True

    # 2. It did NOT retry in a loop
    # Result logged as Uncertain
    db_mock.add.assert_called_once()
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Uncertain"
    assert "Manual review required" in logged_res.error_message

    # 3. Attendance record marked for manual review
    assert att_rec.status == "Needs Review"


@pytest.mark.asyncio
async def test_form_validation_rejection_after_submit():
    """
    Requirement 5: If the form explicitly rejects the submission with a visible validation error,
    it is marked as Failed (not uncertain, and no duplicate retries).
    """
    engine, db_mock, att_rec = create_mock_engine(mode="production")
    page = MockPlaywrightPage(submission_behavior="rejection")
    current_page_holder = [page]
    context_mock = AsyncMock()

    await engine._process_single_record(
        context_mock,
        VALID_RECORD,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    assert page.submit_clicked is True
    db_mock.add.assert_called_once()
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Failed"
    assert "Form rejected submission" in logged_res.error_message
    assert att_rec.status == "Failed"


@pytest.mark.asyncio
async def test_exact_dropdown_verification_blocks_villa1_vs_villa10():
    """
    Requirement 1: Exact dropdown verification.
    Substrings like 'VILLA 1' matching 'VILLA 10' must be strictly rejected.
    """
    engine, db_mock, att_rec = create_mock_engine(mode="production")
    page = MockPlaywrightPage(dropdown_options={
        "worker_name": ["CARPENTER NARESH"],
        "project_name": ["VILLA 10"],
        "boq_category": ["CARPENTRY"],
    })
    # Set valid values for all fields except project_name
    page.containers["attendance_date"].day_input._value = "15"
    page.containers["attendance_date"].month_input._value = "04"
    page.containers["worker_name"].dropdown_btn._text = "CARPENTER NARESH"
    page.containers["boq_category"].dropdown_btn._text = "CARPENTRY"
    page.containers["duration"].main_input._value = "8.0"
    page.containers["description"].main_input._value = "Door framing works"
    page.containers["project_name"].dropdown_btn._text = "VILLA 10"

    rec = dict(VALID_RECORD)
    rec["project_name"] = "VILLA 1"

    # Direct verification test: confirm _verify_fields raises ValueError on substring match
    with pytest.raises(ValueError) as excinfo:
        await engine._verify_fields(page, rec, VALID_FIELD_MAP)

    assert "project_name" in str(excinfo.value)
    assert "Pre-submission verification failed" in str(excinfo.value)
    assert "VILLA 1" in str(excinfo.value)


@pytest.mark.asyncio
async def test_exact_dropdown_verification_blocks_incorrect_selected_value():
    """
    Requirement 1: Exact dropdown verification.
    Verify the actual selected value, not the entire question container text.
    If an incorrect value is selected, fail before submit.
    """
    engine, db_mock, att_rec = create_mock_engine(mode="production")
    page = MockPlaywrightPage(dropdown_options={
        "worker_name": ["PAINTER RAMESH"],
        "project_name": ["VILLA 101"],
        "boq_category": ["CARPENTRY"],
    })
    page.containers["worker_name"].dropdown_btn._text = "PAINTER RAMESH"

    rec = dict(VALID_RECORD)
    rec["worker_name"] = "CARPENTER NARESH"

    current_page_holder = [page]
    context_mock = AsyncMock()

    await engine._process_single_record(
        context_mock,
        rec,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    assert page.submit_clicked is False
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Failed"
    assert "worker_name" in logged_res.error_message


@pytest.mark.asyncio
async def test_strict_date_verification_blocks_transposed_inputs():
    """
    Requirement 2: Strict date verification.
    Verify day against Day input and month against Month input separately.
    Transposed inputs (Month in Day input, Day in Month input) must fail verification.
    """
    engine, db_mock, att_rec = create_mock_engine(mode="production")
    page = MockPlaywrightPage()

    # Record expects Day=05, Month=12 ("05/12")
    rec = dict(VALID_RECORD)
    rec["attendance_date"] = "05/12"

    # Simulate transposed input: Day input contains "12", Month input contains "05"
    async def transposed_fill(val):
        pass  # keep transposed values
    page.containers["attendance_date"].day_input._value = "12"
    page.containers["attendance_date"].month_input._value = "05"
    page.containers["attendance_date"].day_input.fill = transposed_fill
    page.containers["attendance_date"].month_input.fill = transposed_fill

    current_page_holder = [page]
    context_mock = AsyncMock()

    # Call _verify_fields directly to test strict transposition check
    with pytest.raises(ValueError) as excinfo:
        await engine._verify_fields(page, rec, VALID_FIELD_MAP)

    assert "attendance_date" in str(excinfo.value)
    assert "Day input has '12' (expected '05')" in str(excinfo.value)


@pytest.mark.asyncio
async def test_strict_date_verification_fails_safely_on_unknown_input_identity():
    """
    Requirement 2: Strict date verification.
    If field identity cannot be established reliably (no accessible labels for Day/Month),
    fail safely instead of assuming input order.
    """
    engine, db_mock, att_rec = create_mock_engine(mode="production")
    page = MockPlaywrightPage()

    # Remove accessible labels and placeholders from date inputs
    page.containers["attendance_date"].day_input._aria_label = ""
    page.containers["attendance_date"].month_input._aria_label = ""

    current_page_holder = [page]
    context_mock = AsyncMock()

    await engine._process_single_record(
        context_mock,
        VALID_RECORD,
        VALID_FIELD_MAP,
        "http://test-form",
        current_page_holder,
    )

    assert page.submit_clicked is False
    logged_res = db_mock.add.call_args[0][0]
    assert logged_res.status == "Failed"
    assert "Cannot reliably identify Day and Month inputs" in logged_res.error_message


@pytest.mark.asyncio
async def test_outer_recovery_does_not_retry_after_submit_attempt():
    """
    Requirement 3: Prevent duplicate submission during outer recovery.
    Ensure browser-context recovery cannot blindly rerun the same record after a Submit click.
    If submission state is uncertain, record Needs Review / Uncertain and do NOT retry.
    """
    engine, db_mock, att_rec = create_mock_engine(mode="production")

    page = MockPlaywrightPage(submission_behavior="error_after_submit")
    async def crash_on_submit():
        page.submit_clicked = True
        raise RuntimeError("Connection closed: browser process disconnected after submit click")

    page.submit_button.click = crash_on_submit

    context_mock = AsyncMock()
    context_mock.pages = [page]
    context_mock.new_page = AsyncMock(return_value=page)
    context_mock.close = AsyncMock()

    records = [VALID_RECORD]

    mock_p = AsyncMock()
    mock_p.chromium.launch_persistent_context = AsyncMock(return_value=context_mock)

    from unittest.mock import patch
    with patch("app.automation.submission_engine.async_playwright") as mock_ap:
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_p
        mock_cm.__aexit__.return_value = None
        mock_ap.return_value = mock_cm

        batch_mock = UploadBatch(id="batch-001", status="Running")
        db_mock.get = AsyncMock(side_effect=lambda model, ident: batch_mock if model == UploadBatch else att_rec)

        await engine.start_batch(records, "http://test-form", field_map=VALID_FIELD_MAP)

    # Verify submit was clicked
    assert page.submit_clicked is True
    # Verify SubmissionResult was logged as Uncertain and att_rec marked Needs Review
    add_calls = [c[0][0] for c in db_mock.add.call_args_list if isinstance(c[0][0], SubmissionResult)]
    assert len(add_calls) == 1
    assert add_calls[0].status == "Uncertain"
    assert "uncertain" in (add_calls[0].error_message or "").lower()
    assert "submit" in (add_calls[0].error_message or "").lower()
    assert att_rec.status == "Needs Review"


@pytest.mark.asyncio
async def test_api_count_only_vs_dry_run_routing():
    """
    Requirement 4: Provide a real browser dry-run.
    Test API routing: count_only mode returns immediately without launching browser;
    dry_run / browser_validation launches the engine with mode='dry_run'.
    """
    from app.api.automation import run_automation, RunRequest
    from unittest.mock import patch

    db_mock = AsyncMock()
    batch_mock = UploadBatch(id="b-1", session_id="s-1", form_url="http://test-form")
    records_mock = [
        AttendanceRecord(id="r-1", batch_id="b-1", status="Pending", project_name="V1"),
        AttendanceRecord(id="r-2", batch_id="b-1", status="Pending", project_name="V2"),
    ]

    def mock_db_execute(stmt):
        m = MagicMock()
        stmt_str = str(stmt).lower()
        if "upload_batches" in stmt_str:
            m.scalar_one_or_none.return_value = batch_mock
        elif "worker_mappings" in stmt_str:
            m.scalars.return_value.all.return_value = []
        else:
            m.scalars.return_value.all.return_value = records_mock
        return m

    db_mock.execute = AsyncMock(side_effect=mock_db_execute)

    # 1. Test count_only mode
    bg_tasks = MagicMock()
    with patch("app.api.automation.check_google_session_status", return_value={"connected": True}):
        res_count = await run_automation(
            RunRequest(batch_id="b-1", mode="count_only"),
            bg_tasks,
            db_mock,
            session_id="s-1",
        )
        assert res_count["mode"] == "count_only"
        assert res_count["count_only"] is True
        assert res_count["would_submit"] == 2

    # 2. Test dry_run mode launches subprocess with --mode dry_run
    with patch("app.api.automation.check_google_session_status", return_value={"connected": True}), \
         patch("subprocess.Popen") as mock_popen:
        res_dry = await run_automation(
            RunRequest(batch_id="b-1", mode="dry_run"),
            bg_tasks,
            db_mock,
            session_id="s-1",
        )
        assert res_dry["mode"] == "dry_run"
        assert res_dry["queued"] == 2
        mock_popen.assert_called_once()
        cmd_args = mock_popen.call_args[0][0]
        assert "--mode" in cmd_args
        idx = cmd_args.index("--mode")
        assert cmd_args[idx + 1] == "dry_run"


@pytest.mark.asyncio
async def test_status_endpoint_reports_needs_review_and_terminal_progress():
    """
    Requirement 5: Fix status and progress reporting.
    Count Needs Review / Uncertain separately from Submitted, Failed, and Pending.
    Ensure batch progress reaches 100% terminal state when completed.
    Never count uncertain submissions as successful.
    """
    from app.api.automation import get_batch_status

    db_mock = AsyncMock()
    batch_mock = UploadBatch(id="b-1", session_id="s-1", status="Completed")

    # 4 records: 2 Submitted, 1 Failed, 1 Needs Review
    records = [
        AttendanceRecord(id="r-1", batch_id="b-1", status="Submitted"),
        AttendanceRecord(id="r-2", batch_id="b-1", status="Submitted"),
        AttendanceRecord(id="r-3", batch_id="b-1", status="Failed"),
        AttendanceRecord(id="r-4", batch_id="b-1", status="Needs Review"),
    ]

    def mock_db_execute(stmt):
        m = MagicMock()
        stmt_str = str(stmt).lower()
        if "upload_batches" in stmt_str:
            m.scalar_one_or_none.return_value = batch_mock
        elif "attendance_records" in stmt_str:
            m.scalars.return_value.all.return_value = records
        else:
            m.scalars.return_value.all.return_value = []
        return m

    db_mock.execute = AsyncMock(side_effect=mock_db_execute)

    status_data = await get_batch_status("b-1", db_mock, session_id="s-1")

    # Submitted MUST be exactly 2 (never counting Needs Review)
    assert status_data["submitted"] == 2
    # Failed is 1
    assert status_data["failed"] == 1
    # Needs review is 1
    assert status_data["needs_review"] == 1
    assert status_data["uncertain"] == 1
    # Pending is 0
    assert status_data["pending"] == 0
    # Total is 4
    assert status_data["total"] == 4
    # Progress reaches 100% terminal state
    assert status_data["progress_pct"] == 100

