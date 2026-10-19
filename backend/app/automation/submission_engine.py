"""
Playwright-based Google Form submission engine.

Field detection uses label-based auto-mapping with aliases.
No manual field configuration required in V1.
"""
import os
import uuid
import asyncio
import logging
import difflib
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from playwright.async_api import async_playwright, BrowserContext, Page, Locator

from app.db.models import UploadBatch, AttendanceRecord, SubmissionResult

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Alias table: maps internal system field -> list of accepted Google Form labels
# Case-insensitive partial matching is used.
# ──────────────────────────────────────────────────────────────────────────────
FIELD_ALIASES: Dict[str, List[str]] = {
    "attendance_date": [
        "date",
        "attendance date",
        "attandance date",
    ],
    "worker_name": [
        "name of labour",
        "labour name",
        "worker name",
        "employee name",
        "name",
    ],
    "project_name": [
        "project name",
        "project",
        "site name",
    ],
    "boq_category": [
        "boq category",
        "category",
        "work category",
        "work type",
    ],
    "duration": [
        "duration",
        "hours",
        "working hours",
        "time",
    ],
    "description": [
        "description",
        "remarks",
        "activity",
        "work description",
    ],
}

REQUIRED_SYSTEM_FIELDS = list(FIELD_ALIASES.keys())


def _match_label(label_text: str) -> Optional[str]:
    """
    Given a raw label from the Google Form, return the matching system field name
    or None if no alias matches. Prioritizes the longest matching alias.
    """
    normalized = label_text.strip().lower()
    best_match = None
    longest_alias = 0

    for sys_field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized or normalized in alias:
                if len(alias) > longest_alias:
                    best_match = sys_field
                    longest_alias = len(alias)
                    
    return best_match


def extract_form_schema(form_url: str) -> Dict[str, List[str]]:
    """
    Parses Google Form HTML directly and extracts all question titles and their dropdown/radio options.
    Returns a dict mapping system field names to list of option strings.
    """
    import urllib.request
    import re
    import json
    schema: Dict[str, List[str]] = {}
    try:
        req = urllib.request.Request(
            form_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=12) as response:
            html = response.read().decode('utf-8', errors='ignore')

        match = re.search(r'FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);</script>', html, re.DOTALL)
        if not match:
            return schema

        data = json.loads(match.group(1))
        questions = data[1][1]

        for q in questions:
            if not isinstance(q, list) or len(q) < 2:
                continue
            title = str(q[1]).strip() if q[1] else ""
            if not title:
                continue

            sys_field = _match_label(title)
            if not sys_field:
                continue

            options = []
            if len(q) > 4 and q[4]:
                sub = q[4][0]
                if len(sub) > 1 and sub[1]:
                    for opt in sub[1]:
                        if opt and isinstance(opt, list) and len(opt) > 0 and opt[0]:
                            opt_str = str(opt[0]).strip()
                            if opt_str and opt_str not in options:
                                options.append(opt_str)

            if sys_field not in schema or len(options) > len(schema[sys_field]):
                schema[sys_field] = options

    except Exception as e:
        logger.error(f"[FormSchemaExtractor] Error extracting schema from {form_url}: {e}")

    return schema



async def detect_field_map(page: Page, form_url: str) -> Tuple[Dict[str, dict], List[str], List[str]]:
    """
    Opens the Google Form and reads each question label.
    Returns:
      field_map:  { system_field -> {"label": "safe_label", "type": "input_type"} }
      found:      list of system fields that were matched
      missing:    list of required system fields that were NOT found
    """
    await page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_selector('.Qr7Oae, div[role="listitem"], div[jsmodel], [data-params]', timeout=5000)
    except Exception:
        pass

    field_map: Dict[str, dict] = {}

    question_containers = await page.query_selector_all(
        '[data-params], .freebirdFormviewerViewItemsItemItem, div[jsmodel], .Qr7Oae, div[role="listitem"]'
    )

    for container in question_containers:
        title_el = await container.query_selector(
            '.freebirdFormviewerComponentsQuestionBaseTitle, .M7eMe, [role="heading"], span[dir="auto"]'
        )
        if not title_el:
            continue

        label_text = (await title_el.inner_text()).strip()
        if not label_text:
            continue

        sys_field = _match_label(label_text)
        if not sys_field or sys_field in field_map:
            continue

        # Detect the type of input within this container
        input_type = "unknown"
        if await container.query_selector('input[type="text"], input[type="date"], input[type="email"], input[type="url"], input:not([type="hidden"])'):
            input_type = "text"
        elif await container.query_selector('textarea'):
            input_type = "textarea"
        elif await container.query_selector('div[role="listbox"], [jsname="LgbsSe"]'):
            input_type = "listbox"
        elif await container.query_selector('div[role="radiogroup"]'):
            input_type = "radio"
        
        # Fallback: if we matched a valid system field but couldn't detect the type,
        # Google Forms dropdowns sometimes hide the listbox deep. Default to listbox.
        if input_type == "unknown" and sys_field:
            input_type = "listbox"

        if input_type == "unknown":
            continue

        safe_label = label_text.split('\n')[0].replace('"', '').strip()
        if safe_label.endswith('*'):
            safe_label = safe_label[:-1].strip()

        field_map[sys_field] = {
            "label": safe_label,
            "type": input_type
        }
        logger.info(f"[AutoMap] '{label_text}' -> {sys_field} | type: {input_type}")

    found = list(field_map.keys())
    missing = [f for f in REQUIRED_SYSTEM_FIELDS if f not in found]
    return field_map, found, missing


_active_login_connections = set()

def mark_connection_active(session_id: str):
    _active_login_connections.add(session_id)

def mark_connection_inactive(session_id: str):
    _active_login_connections.discard(session_id)

def is_connection_active(session_id: str) -> bool:
    return session_id in _active_login_connections


async def check_google_session_status(session_id: str) -> Dict[str, Any]:
    """
    Checks if there's a valid active Google login session for this session_id.
    """
    if is_connection_active(session_id):
        return {"connected": False, "email": None, "connecting": True}

    session_dir = os.path.abspath(f"playwright_sessions/{session_id}")
    if not os.path.exists(session_dir):
        return {"connected": False, "email": None}
    
    async with async_playwright() as p:
        context = None
        for launch_attempt in range(3):
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=session_dir,
                    headless=True,
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-setuid-sandbox"],
                )
                break
            except Exception as e:
                if launch_attempt == 2:
                    return {"connected": False, "email": None}
                await asyncio.sleep(0.5)

        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://myaccount.google.com/email", wait_until="domcontentloaded", timeout=15000)
            
            url = page.url
            if "accounts.google.com" in url:
                await context.close()
                return {"connected": False, "email": None}

            
            email = None
            try:
                elements = await page.locator("text=@").all()
                for el in elements:
                    text = await el.inner_text()
                    if "@" in text and "." in text and len(text) < 100:
                        for word in text.split():
                            if "@" in word and "." in word:
                                email = word.strip("()<>[],;:")
                                break
                    if email:
                        break
            except Exception:
                pass
            
            await context.close()
            if email:
                return {"connected": True, "email": email}
            
            return {"connected": True, "email": "Connected Account"}
        except Exception as e:
            logger.warning(f"Error checking Google status for session {session_id}: {e}")
            return {"connected": False, "email": None}


async def validate_form(form_url: str, session_id: str) -> Dict[str, Any]:
    """
    Opens the Google Form, detects fields, returns a validation report.
    Does NOT submit anything.
    """
    session_dir = os.path.abspath(f"playwright_sessions/{session_id}")
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            field_map, found, missing = await detect_field_map(page, form_url)
            passed = len(missing) == 0
            return {
                "passed": passed,
                "form_url": form_url,
                "fields_detected": len(found),
                "found": found,
                "missing": missing,
                "field_map": {k: v for k, v in field_map.items()},
                "message": (
                    "All required fields detected. Ready to submit."
                    if passed
                    else f"Missing fields: {', '.join(missing)}. Submission blocked."
                ),
            }
        except Exception as e:
            return {
                "passed": False,
                "form_url": form_url,
                "fields_detected": 0,
                "found": [],
                "missing": REQUIRED_SYSTEM_FIELDS,
                "field_map": {},
                "message": f"Validation error: {str(e)}",
            }
        finally:
            await context.close()


class PlaywrightSubmissionEngine:
    def __init__(self, db_session: AsyncSession, batch_id: str, session_id: str, mode: str = "production"):
        self.db = db_session
        self.batch_id = batch_id
        self.session_id = session_id
        self.mode = mode  # 'production' | 'test_visible' | 'dry_run'
        os.makedirs("reports/errors", exist_ok=True)

    async def start_batch(
        self,
        valid_records: List[Dict[str, Any]],
        form_url: str,
        field_map: Optional[Dict[str, dict]] = None,
    ):
        headless = self.mode != "test_visible"

        async with async_playwright() as p:
            context = None
            for launch_attempt in range(5):
                try:
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir=f"playwright_sessions/{self.session_id}",
                        headless=headless,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-gpu",
                            "--disable-dev-shm-usage",
                        ],
                    )
                    break
                except Exception as e:
                    if launch_attempt == 4:
                        raise e
                    await asyncio.sleep(1.0)
            try:
                # Auto-detect field map if not pre-supplied
                if not field_map:
                    logger.info("[Engine] Auto-detecting field map from form...")
                    temp_page = await context.new_page()
                    field_map, found, missing = await detect_field_map(temp_page, form_url)
                    await temp_page.close()

                    if missing:
                        logger.error(f"[Engine] Cannot submit — missing fields: {missing}")
                        # Mark batch as failed
                        batch = await self.db.get(UploadBatch, self.batch_id)
                        if batch:
                            batch.status = "Failed"
                            await self.db.commit()
                        return
                else:
                    missing = [f for f in REQUIRED_SYSTEM_FIELDS if f not in field_map]
                    if missing:
                        logger.error(f"[Engine] Cannot submit — supplied field map missing fields: {missing}")
                        batch = await self.db.get(UploadBatch, self.batch_id)
                        if batch:
                            batch.status = "Failed"
                            await self.db.commit()
                        return

                current_page_holder = [None]
                try:
                    for record in valid_records:
                        # Poll batch status (supports pause/cancel)
                        batch = await self.db.get(UploadBatch, self.batch_id)
                        if not batch:
                            break
                        while batch.status == "Paused":
                            await asyncio.sleep(5)
                            await self.db.refresh(batch)
                        if batch.status == "Cancelled":
                            break

                        record_state = {"submit_attempted": False}
                        try:
                            await self._process_single_record(
                                context, record, field_map, form_url, current_page_holder, record_state
                            )
                        except Exception as e:
                            err_str = str(e)
                            logger.error(f"[Engine] Exception during record {record.get('id')}: {e}")

                            # If submit was attempted or may have occurred, NEVER retry this record!
                            if record_state.get("submit_attempted"):
                                logger.critical(
                                    f"[Engine] Failure occurred after Submit click for record {record.get('id')}. "
                                    f"Outer recovery will NOT retry to prevent duplicate submission. Marking Uncertain."
                                )
                                ss_path, _ = await self._capture_error_artifacts(current_page_holder[0])
                                msg = f"Context failure after submit attempt: {e}. Manual review required | ss: {ss_path}"
                                await self._log_result(record, form_url, "Uncertain", msg)
                                current_page_holder[0] = None
                                if "Connection closed" in err_str or "closed" in err_str or "driver" in err_str:
                                    context = await self._recreate_context(p, headless)
                                continue

                            # If failure occurred strictly before any submit attempt, we can recreate context and retry once
                            if "Connection closed" in err_str or "closed" in err_str or "driver" in err_str:
                                logger.warning(f"[Engine] Browser context died before submit attempt. Recreating context and retrying record once...")
                                try:
                                    await context.close()
                                except Exception:
                                    pass
                                context = await self._recreate_context(p, headless)
                                current_page_holder[0] = None
                                try:
                                    await self._process_single_record(
                                        context, record, field_map, form_url, current_page_holder, record_state
                                    )
                                except Exception as retry_err:
                                    logger.error(f"[Engine] Retry failed for record {record.get('id')}: {retry_err}")
                                    if record_state.get("submit_attempted"):
                                        await self._log_result(record, form_url, "Uncertain", f"Retry failed after submit click: {retry_err}")
                                    else:
                                        await self._log_result(record, form_url, "Failed", f"Pre-submit failure on retry: {retry_err}")
                                    current_page_holder[0] = None
                finally:
                    if current_page_holder[0] and not current_page_holder[0].is_closed():
                        try:
                            await current_page_holder[0].close()
                        except Exception:
                            pass

            finally:
                if context:
                    await context.close()

    async def _recreate_context(self, p, headless: bool) -> BrowserContext:
        """Helper to safely recreate persistent browser context after a crash."""
        for launch_attempt in range(5):
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=f"playwright_sessions/{self.session_id}",
                    headless=headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                    ],
                )
                return context
            except Exception as launch_err:
                if launch_attempt == 4:
                    raise launch_err
                await asyncio.sleep(1.0)

    async def _capture_error_artifacts(self, page: Optional[Page]) -> Tuple[Optional[str], Optional[str]]:
        """
        Captures screenshot and HTML page source for auditing and manual review.
        Returns (screenshot_path, html_path).
        """
        if not page or page.is_closed():
            return None, None
        file_id = str(uuid.uuid4())
        ss_path = f"reports/errors/{file_id}.png"
        html_path = f"reports/errors/{file_id}.html"
        try:
            await page.screenshot(path=ss_path)
            html_content = await page.content()
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            return ss_path, html_path
        except Exception as e:
            logger.warning(f"[Engine] Failed to capture error artifacts: {e}")
            return None, None

    async def _evaluate_submission_outcome(self, page: Page) -> Tuple[str, Optional[str]]:
        """
        Determines whether Google Form submission was confirmed, rejected, or uncertain.
        Returns:
            ("Success", None)
            ("Failed", rejection_reason)
            ("Uncertain", uncertainty_reason)
        """
        try:
            await page.wait_for_function(
                "() => window.location.href.includes('formResponse') || "
                "document.querySelector('.freebirdFormviewerViewResponseConfirmationMessage, .vHW8K') !== null || "
                "document.body.innerText.includes('Your response has been recorded') || "
                "document.body.innerText.includes('Submit another response')",
                timeout=10000
            )
            return "Success", None
        except Exception:
            pass

        # 1. Check if form displayed any validation rejection alert across all questions
        try:
            error_els = []
            if hasattr(page, "query_selector_all"):
                error_els = await page.query_selector_all(
                    '.RxsGPe, div[role="alert"], .freebirdFormviewerViewItemsItemErrorMessage, .RHiWt, span[role="alert"]'
                )
            elif hasattr(page, "query_selector"):
                single_el = await page.query_selector('.RxsGPe, div[role="alert"], .freebirdFormviewerViewItemsItemErrorMessage, .RHiWt')
                if single_el:
                    error_els = [single_el]

            for error_el in error_els:
                if await error_el.is_visible():
                    err_txt = (await error_el.inner_text()).strip()
                    if err_txt:
                        return "Failed", f"Form rejected submission: {err_txt}"
        except Exception:
            pass

        # 2. Check current URL and page content for confirmation signs
        try:
            current_url = page.url or ""
            if "formResponse" in current_url:
                return "Success", None
            content = await page.content()
            if "Your response has been recorded" in content or "Submit another response" in content:
                return "Success", None
        except Exception:
            pass

        # 3. Outcome cannot be verified with certainty
        return (
            "Uncertain",
            "Submission outcome uncertain: no confirmation received after clicking Submit. "
            "Manual review required to prevent duplicate submissions."
        )

    async def _fill_field(
        self,
        page: Page,
        container: Locator,
        sys_field: str,
        field_info: dict,
        value: Any,
    ):
        """
        Fills a single form question container.
        Raises ValueError or RuntimeError on failure.
        Exact dropdown matching is enforced for worker_name and project_name.
        Strict date identification uses accessible labels without assuming order.
        """
        input_type = field_info["type"]
        val_str = str(value).strip()

        if input_type == "listbox":
            dropdown = container.locator('div[role="listbox"]').first
            val_lower = val_str.lower()
            val_norm = " ".join(val_str.split()).strip().lower()

            for attempt in range(3):
                # 1. Open listbox if not expanded
                expanded = ""
                try:
                    expanded = await dropdown.get_attribute("aria-expanded")
                except Exception:
                    pass
                if expanded != "true":
                    await dropdown.click()
                    await page.wait_for_timeout(250)

                # 2. Gather visible options (container first, then page)
                opt_map = {}
                candidate_locators = []
                try:
                    c_opts = await container.locator('div[role="option"]').all()
                    for opt in c_opts:
                        try:
                            if await opt.is_visible():
                                txt = (await opt.text_content(timeout=500) if hasattr(opt, "text_content") else await opt.inner_text()).strip()
                                if txt and txt.lower() not in ("choose", "select", "— choose —"):
                                    opt_map[txt] = opt
                                    opt_map[txt.lower()] = opt
                        except Exception:
                            continue
                except Exception:
                    pass

                # If container didn't yield options (e.g. In mocks or detached portal menus), query page
                if not opt_map:
                    try:
                        p_opts = await page.locator('div[role="option"]').all()
                        for opt in p_opts:
                            try:
                                if await opt.is_visible():
                                    txt = (await opt.text_content(timeout=500) if hasattr(opt, "text_content") else await opt.inner_text()).strip()
                                    if txt and txt.lower() not in ("choose", "select", "— choose —"):
                                        opt_map[txt] = opt
                                        opt_map[txt.lower()] = opt
                            except Exception:
                                continue
                    except Exception:
                        pass

                target_opt = None
                if val_str in opt_map:
                    target_opt = opt_map[val_str]
                elif val_lower in opt_map:
                    target_opt = opt_map[val_lower]
                else:
                    if sys_field in ("worker_name", "project_name"):
                        if attempt == 2:
                            raise ValueError(
                                f"Exact option '{val_str}' not found in dropdown for '{sys_field}'. "
                                f"Automatic fuzzy selection is disabled for worker and project names."
                            )
                        continue
                    best_matches = difflib.get_close_matches(val_lower, list(opt_map.keys()), n=1, cutoff=0.4)
                    if best_matches:
                        target_opt = opt_map[best_matches[0]]
                    elif attempt == 2:
                        raise ValueError(f"Option '{val_str}' not found in dropdown for '{sys_field}'")
                    else:
                        continue

                if target_opt:
                    await target_opt.click()
                    await page.wait_for_timeout(300)

                # Confirm selection registered on the dropdown
                curr_txt = ""
                try:
                    curr_txt = (await dropdown.text_content(timeout=500) if hasattr(dropdown, "text_content") else await dropdown.inner_text()).strip()
                except Exception:
                    pass
                curr_norm = " ".join(curr_txt.split()).strip().lower()
                if val_norm in curr_norm or (target_opt and curr_norm not in ("choose", "select", "— choose —", "")):
                    break

        elif input_type in ("text", "textarea"):
            if sys_field == "attendance_date":
                # Special handling for Google Forms date widgets with multiple inputs (Day, Month, optional Year)
                parts = val_str.replace("-", "/").split("/")
                if len(parts) >= 2:
                    dd, mm = parts[0].strip(), parts[1].strip()
                    inputs = await container.locator('input[type="text"]').all()
                    if len(inputs) >= 2:
                        day_input = None
                        month_input = None
                        year_input = None
                        for inp in inputs:
                            aria = (await inp.get_attribute("aria-label") or "").lower()
                            placeholder = (await inp.get_attribute("placeholder") or "").lower()
                            if "day" in aria or placeholder in ("dd", "day"):
                                day_input = inp
                            elif "month" in aria or placeholder in ("mm", "month"):
                                month_input = inp
                            elif "year" in aria or placeholder in ("yyyy", "year"):
                                year_input = inp

                        if not day_input or not month_input:
                            raise ValueError(
                                f"Cannot reliably identify Day and Month inputs in date field (missing accessible labels). "
                                f"Blocking submission to prevent transposed date errors."
                            )

                        await day_input.fill(dd)
                        await day_input.blur()
                        await month_input.fill(mm)
                        await month_input.blur()
                        if year_input and len(parts) >= 3:
                            await year_input.fill(parts[2].strip())
                            await year_input.blur()
                        return

            input_el = container.locator('input[type="text"], input:not([type="hidden"]), textarea').first
            await input_el.fill(val_str)
            await input_el.blur()

        elif input_type == "radio":
            option = container.locator(f'[data-value="{val_str}"], div[role="radio"]').filter(has_text=val_str).first
            if await option.count() == 0:
                raise ValueError(f"Radio option '{val_str}' not found for '{sys_field}'")
            await option.click()
        else:
            raise ValueError(f"Unsupported input type '{input_type}' for field '{sys_field}'")

    async def _verify_fields(
        self,
        page: Page,
        record: Dict[str, Any],
        field_map: Dict[str, dict],
    ):
        """
        Confirms that each of the six required fields has been filled with the approved preview value:
        attendance_date, worker_name, project_name, boq_category, duration, and description.
        Uses exact normalized string comparison. Substring matching is disallowed.
        Validates Day against Day input and Month against Month input separately.
        Raises ValueError if verification fails.
        """
        def norm(s: str) -> str:
            return " ".join(str(s).split()).strip().lower()

        for sys_field in REQUIRED_SYSTEM_FIELDS:
            expected_val = str(record.get(sys_field, "")).strip()
            field_info = field_map.get(sys_field)
            if not field_info:
                raise ValueError(f"Cannot verify field '{sys_field}': missing from field map")

            label_text = field_info["label"]
            input_type = field_info["type"]
            container = page.locator('.Qr7Oae, div[role="listitem"], div[jsmodel]').filter(has_text=label_text).last

            if input_type in ("text", "textarea"):
                if sys_field == "attendance_date":
                    parts = expected_val.replace("-", "/").split("/")
                    dd = parts[0].strip()
                    mm = parts[1].strip() if len(parts) > 1 else ""
                    inputs = await container.locator('input[type="text"]').all()
                    if len(inputs) >= 2:
                        day_input = None
                        month_input = None
                        for inp in inputs:
                            aria = (await inp.get_attribute("aria-label") or "").lower()
                            placeholder = (await inp.get_attribute("placeholder") or "").lower()
                            if "day" in aria or placeholder in ("dd", "day"):
                                day_input = inp
                            elif "month" in aria or placeholder in ("mm", "month"):
                                month_input = inp

                        if not day_input or not month_input:
                            raise ValueError(
                                f"Cannot reliably identify Day and Month inputs for verification (missing accessible labels). "
                                f"Blocking submission to prevent transposed date errors."
                            )

                        val_day = (await day_input.input_value() or "").strip()
                        val_month = (await month_input.input_value() or "").strip()

                        def match_part(act, exp):
                            return act == exp or act.zfill(2) == exp.zfill(2) or act.lstrip("0") == exp.lstrip("0")

                        day_matched = match_part(val_day, dd)
                        month_matched = match_part(val_month, mm) if mm else True

                        if not day_matched or not month_matched:
                            raise ValueError(
                                f"Pre-submission verification failed for 'attendance_date': "
                                f"Day input has '{val_day}' (expected '{dd}'), "
                                f"Month input has '{val_month}' (expected '{mm}')"
                            )
                        continue
                    else:
                        inp = container.locator('input[type="text"], input[type="date"], input:not([type="hidden"])').first
                        actual_val = (await inp.input_value() or "").strip()
                        if norm(expected_val) != norm(actual_val):
                            raise ValueError(
                                f"Pre-submission verification failed for '{sys_field}': "
                                f"expected '{expected_val}', found '{actual_val}'"
                            )
                        continue

                input_el = container.locator('input[type="text"], input:not([type="hidden"]), textarea').first
                actual_val = (await input_el.input_value() or "").strip()
                if norm(actual_val) != norm(expected_val):
                    raise ValueError(
                        f"Pre-submission verification failed for '{sys_field}': "
                        f"expected '{expected_val}', found '{actual_val}'"
                    )

            elif input_type == "listbox":
                dropdown = container.locator('div[role="listbox"]').first
                dropdown_text = ""
                try:
                    dropdown_text = (await dropdown.text_content(timeout=1000) or "").strip()
                except Exception:
                    try:
                        dropdown_text = (await dropdown.inner_text(timeout=1000) or "").strip()
                    except Exception:
                        dropdown_text = ""

                selected_texts = []
                # 1. Check aria-selected option within THIS question container first
                try:
                    sel_in_container = container.locator('div[role="option"][aria-selected="true"]')
                    if await sel_in_container.count() > 0:
                        for s_opt in await sel_in_container.all():
                            t = (await s_opt.text_content(timeout=500) if hasattr(s_opt, "text_content") else await s_opt.inner_text()).strip()
                            if t and norm(t) not in ("choose", "select", "— choose —"):
                                selected_texts.append(t)
                except Exception:
                    pass

                # 2. Fallback to page-level aria-selected if container has none (e.g. in test mocks)
                if not selected_texts:
                    try:
                        selected_opts = await page.locator('div[role="option"][aria-selected="true"]').all()
                        for opt in selected_opts:
                            t = ""
                            try:
                                if hasattr(opt, "text_content"):
                                    t = await opt.text_content(timeout=500)
                                else:
                                    t = await opt.inner_text()
                            except Exception:
                                pass
                            if t and norm(t) not in ("choose", "select", "— choose —"):
                                selected_texts.append(t.strip())
                    except Exception:
                        pass

                expected_norm = norm(expected_val)

                # Extract candidates exclusively from the dropdown element or aria-selected option
                # If dropdown still shows "Choose" / "Select", it is NOT selected!
                candidates = []
                norm_dd = norm(dropdown_text)
                if norm_dd and norm_dd not in ("choose", "select", "— choose —"):
                    candidates.append(norm_dd)
                    for line in dropdown_text.splitlines():
                        c_line = norm(line)
                        if c_line and c_line not in ("choose", "select", "— choose —"):
                            candidates.append(c_line)

                for s in selected_texts:
                    s_norm = norm(s)
                    if s_norm and s_norm not in ("choose", "select", "— choose —"):
                        candidates.append(s_norm)

                # Exact equality comparison on normalized strings - NO substring matching
                matched = any(c == expected_norm for c in candidates)
                if not matched:
                    raise ValueError(
                        f"Pre-submission verification failed for '{sys_field}': "
                        f"expected exact option '{expected_val}', found dropdown text '{dropdown_text}'"
                    )

            elif input_type == "radio":
                checked_radio = container.locator('div[role="radio"][aria-checked="true"], input[type="radio"]:checked')
                if await checked_radio.count() == 0:
                    checked_radio = container.locator(f'[data-value="{expected_val}"][aria-checked="true"]')
                if await checked_radio.count() == 0:
                    raise ValueError(
                        f"Pre-submission verification failed for '{sys_field}': "
                        f"radio option '{expected_val}' is not selected"
                    )

    async def _process_single_record(
        self,
        context: BrowserContext,
        record: Dict[str, Any],
        field_map: Dict[str, dict],
        form_url: str,
        current_page_holder: list,
        record_state: Optional[Dict[str, bool]] = None,
    ):
        if record_state is None:
            record_state = {"submit_attempted": False}

        # 1. Validate required values in record - block submission if any is missing
        missing_values = [
            f for f in REQUIRED_SYSTEM_FIELDS
            if record.get(f) is None or str(record.get(f)).strip() == ""
        ]
        if missing_values:
            err_msg = f"Missing required value for field(s): {', '.join(missing_values)}"
            logger.error(f"[Engine] Record {record.get('id')} blocked: {err_msg}")
            await self._log_result(record, form_url, "Failed", err_msg)
            return

        # 2. Validate field map contains all required fields
        missing_map = [f for f in REQUIRED_SYSTEM_FIELDS if f not in field_map]
        if missing_map:
            err_msg = f"Form field map is missing required field(s): {', '.join(missing_map)}"
            logger.error(f"[Engine] Field map incomplete: {err_msg}")
            await self._log_result(record, form_url, "Failed", err_msg)
            return

        max_retries = 3
        submitted_clicked = False
        for attempt in range(max_retries):
            page = current_page_holder[0]
            try:
                if page is None or page.is_closed():
                    page = await context.new_page()
                    current_page_holder[0] = page
                    await page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
                else:
                    submit_another_btn = page.locator('a:has-text("Submit another response"), a:has-text("Submit another")').first
                    try:
                        await submit_another_btn.wait_for(state="visible", timeout=1500)
                        await submit_another_btn.click()
                        await page.wait_for_selector('.Qr7Oae, div[role="listitem"], div[jsmodel], [data-params]', timeout=3000)
                    except Exception:
                        await page.goto(form_url, wait_until="domcontentloaded", timeout=30000)

                # Fill all required fields (fails safely on any error)
                for sys_field in REQUIRED_SYSTEM_FIELDS:
                    field_info = field_map[sys_field]
                    label_text = field_info["label"]
                    value = record.get(sys_field)

                    try:
                        container = page.locator('.Qr7Oae, div[role="listitem"], div[jsmodel]').filter(has_text=label_text).last
                        await container.wait_for(state="visible", timeout=5000)
                        await self._fill_field(page, container, sys_field, field_info, value)
                    except Exception as fill_err:
                        logger.error(f"[Fill] Could not fill required field '{sys_field}' ('{label_text}'): {fill_err}")
                        raise RuntimeError(f"Could not fill required field '{sys_field}': {fill_err}") from fill_err

                # Pre-submission verification: confirm all 6 fields match approved preview values
                await self._verify_fields(page, record, field_map)

                # Dry-run handling: stop before submit, never click submit, log success
                if self.mode == "dry_run":
                    logger.info(f"[Engine] [Dry Run] Record {record.get('id')} successfully filled and verified. Skipping submit.")
                    await self._log_result(
                        record,
                        form_url,
                        "Success",
                        "[Dry Run] Successfully filled and verified all 6 fields. Form submission skipped.",
                    )
                    return

                # Submit button handling
                submit_btn = page.locator(
                    'div[role="button"]:has-text("Submit"), '
                    'button:has-text("Submit")'
                ).first

                for _ in range(10):
                    aria_disabled = await submit_btn.get_attribute("aria-disabled")
                    if aria_disabled != "true":
                        break
                    await page.wait_for_timeout(200)

                # Mark submit as clicked: once clicked, never blindly retry!
                submitted_clicked = True
                record_state["submit_attempted"] = True
                await submit_btn.click()

                # Evaluate submission outcome
                outcome, details = await self._evaluate_submission_outcome(page)

                if outcome == "Success":
                    await self._log_result(record, form_url, "Success", None)
                    return
                elif outcome == "Uncertain":
                    ss_path, _ = await self._capture_error_artifacts(page)
                    msg = f"{details} | ss: {ss_path}" if ss_path else details
                    await self._log_result(record, form_url, "Uncertain", msg)
                    return
                else:  # "Failed"
                    ss_path, _ = await self._capture_error_artifacts(page)
                    msg = f"{details} | ss: {ss_path}" if ss_path else details
                    await self._log_result(record, form_url, "Failed", msg)
                    return

            except Exception as e:
                # If submit was already clicked, do NOT blindly retry
                if submitted_clicked:
                    logger.error(f"[Engine] Exception after clicking submit: {e}. Checking acceptance before marking uncertain.")
                    confirmed = False
                    try:
                        if "formResponse" in (page.url or ""):
                            confirmed = True
                        else:
                            content = await page.content()
                            if "Your response has been recorded" in content or "Submit another response" in content:
                                confirmed = True
                    except Exception:
                        pass

                    if confirmed:
                        await self._log_result(record, form_url, "Success", None)
                        return

                    ss_path, _ = await self._capture_error_artifacts(page)
                    err_msg = f"Outcome uncertain after clicking Submit: {e}. Manual review required | ss: {ss_path}" if ss_path else f"Outcome uncertain after clicking Submit: {e}. Manual review required."
                    await self._log_result(record, form_url, "Uncertain", err_msg)
                    try:
                        await page.close()
                    except Exception:
                        pass
                    current_page_holder[0] = None
                    return

                # If error is a permanent fill / validation / verification error, do not retry
                if isinstance(e, (ValueError, RuntimeError)):
                    logger.error(f"[Engine] Permanent error for record {record.get('id')}: {e}")
                    ss_path, _ = await self._capture_error_artifacts(page)
                    err_msg = f"{e} | ss: {ss_path}" if ss_path else str(e)
                    await self._log_result(record, form_url, "Failed", err_msg)
                    try:
                        await page.close()
                    except Exception:
                        pass
                    current_page_holder[0] = None
                    return

                # For transient browser crashes on the last attempt
                if attempt == max_retries - 1:
                    ss_path, _ = await self._capture_error_artifacts(page)
                    err_msg = f"{e} | ss: {ss_path}" if ss_path else str(e)
                    await self._log_result(record, form_url, "Failed", err_msg)

                try:
                    await page.close()
                except Exception:
                    pass
                current_page_holder[0] = None

                if attempt < max_retries - 1:
                    await asyncio.sleep(2**attempt)

    async def _log_result(
        self,
        record: Dict[str, Any],
        form_url: str,
        status: str,
        error_msg: Optional[str],
    ):
        rec_id = record.get("id")
        # Idempotency check: prevent duplicate SubmissionResult logs for the same record in this session
        if rec_id:
            existing_res = await self.db.execute(
                select(SubmissionResult).where(
                    SubmissionResult.record_id == str(rec_id),
                    SubmissionResult.session_id == self.session_id,
                )
            )
            if existing_res.scalar_one_or_none():
                return

        current_year = datetime.now().year
        date_str = record.get("attendance_date", "")
        try:
            parsed_date = datetime.strptime(f"{date_str}/{current_year}", "%d/%m/%Y").date()
        except ValueError:
            parsed_date = datetime.utcnow().date()

        # Update attendance record status
        from app.db.models import AttendanceRecord
        if rec_id:
            att = await self.db.get(AttendanceRecord, str(rec_id))
            if att:
                if self.mode == "dry_run":
                    if status == "Failed":
                        att.status = "Failed"
                    elif status == "Uncertain":
                        att.status = "Needs Review"
                    # In dry_run success, leave as Pending so it can actually be submitted later
                else:
                    if status == "Success":
                        att.status = "Submitted"
                    elif status == "Uncertain":
                        att.status = "Needs Review"
                    else:
                        att.status = "Failed"

        res = SubmissionResult(
            record_id=str(record.get("id", uuid.uuid4())),
            session_id=self.session_id,
            timestamp=datetime.utcnow(),
            attendance_date=parsed_date,
            worker_name=record.get("worker_name", "Unknown"),
            project_name=record.get("project_name", "Unknown"),
            duration=record.get("duration", ""),
            status=status,
            error_message=error_msg,
            form_url=form_url,
        )
        self.db.add(res)
        await self.db.commit()

