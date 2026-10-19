import re
import json
import logging
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, delete
from playwright.async_api import async_playwright, Page

from app.db.models import FormProfile, FormField, FieldMapping, ProjectAlias, AttendanceRecord
from app.automation.submission_engine import detect_field_map, check_google_session_status, extract_form_schema, is_connection_active

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """Removes all non-alphanumeric characters and converts to lowercase."""
    if not name:
        return ""
    return re.sub(r'[^a-zA-Z0-9]', '', name).lower()

def get_tokens(name: str) -> List[str]:
    """Splits string into lowercase alphanumeric tokens."""
    if not name:
        return []
    return [t.lower().strip() for t in re.split(r'[^a-zA-Z0-9]', name) if t.strip()]

class ProjectResolver:
    def __init__(self, db_session: AsyncSession, session_id: str):
        self.db = db_session
        self.session_id = session_id

    async def get_project_catalog(self, form_url: str, force_refresh: bool = False) -> List[str]:
        """
        Retrieves the project names catalog for the given Google Form URL.
        First tries to read from cached FormProfile and FormField options in the DB.
        If not cached (or force_refresh=True), scrapes the form via Playwright and updates cache.
        """
        # 1. Try fetching from DB cache first (if not forcing refresh)
        if not force_refresh:
            # Try to get the mapped field for the current session first
            stmt = select(FormField).join(FormProfile).join(FieldMapping).where(
                FormProfile.url == form_url,
                FormProfile.session_id == self.session_id,
                FieldMapping.system_field == "project_name"
            )
            res = await self.db.execute(stmt)
            field = res.scalars().first()

            if not field:
                stmt = select(FormField).join(FormProfile).where(
                    FormProfile.url == form_url,
                    FormProfile.session_id == self.session_id,
                    FormField.label.like("%project%") | FormField.label.like("%site%")
                )
                res = await self.db.execute(stmt)
                field = res.scalars().first()

            if field and field.options:
                try:
                    options = json.loads(field.options)
                    if options:
                        logger.info(f"[Resolver] Catalog cache HIT for form: {form_url}")
                        return options
                except Exception as e:
                    logger.error(f"[Resolver] Error decoding cached options: {e}")

        # 2. Cache miss or force refresh: Scrape Google Form directly
        logger.info(f"[Resolver] Catalog cache MISS or Refresh. Scraping form: {form_url}")
        scraped_options = await self._scrape_project_options(form_url)
        if not scraped_options:
            scraped_options = []

        # 3. Save / Update cache in DB
        # Look for existing profile for current session
        res_profile = await self.db.execute(
            select(FormProfile).where(
                FormProfile.url == form_url,
                FormProfile.session_id == self.session_id
            )
        )
        profile = res_profile.scalars().first()
        if not profile:
            profile = FormProfile(
                session_id=self.session_id,
                name=f"Profile for {form_url[:30]}...",
                url=form_url
            )
            self.db.add(profile)
            await self.db.flush()

        # Look for project_name FormField or create one
        res_field = await self.db.execute(
            select(FormField).where(
                FormField.profile_id == profile.id,
                FormField.label.like("%project%") | FormField.label.like("%site%")
            )
        )
        field = res_field.scalars().first()
        if not field:
            field = FormField(
                profile_id=profile.id,
                field_id=f"scraped_project_{uuid_hex()[:8]}",
                label="Assigned Project Site",
                field_type="listbox",
                is_required=True
            )
            self.db.add(field)

        field.options = json.dumps(scraped_options)
        await self.db.commit()
        logger.info(f"[Resolver] Cached {len(scraped_options)} projects in DB.")
        return scraped_options


    async def _scrape_project_options(self, form_url: str) -> List[str]:
        """Extracts option labels for the project dropdown directly from Google Form schema, falling back to Playwright."""
        # 1. Fast direct HTML extraction
        try:
            schema = extract_form_schema(form_url)
            proj_opts = schema.get("project_name", [])
            if proj_opts:
                logger.info(f"[Resolver] Direct schema extracted {len(proj_opts)} project options for form {form_url}")
                return proj_opts
        except Exception as e:
            logger.warning(f"[Resolver] Direct HTML schema extraction failed: {e}")

        # 2. Fallback Playwright execution
        from app.api.automation import _playwright_pool
        loop = asyncio.get_running_loop()
        
        options = await loop.run_in_executor(
            _playwright_pool,
            lambda: asyncio.run(self._scrape_playwright_worker(form_url))
        )
        return options


    async def _scrape_playwright_worker(self, form_url: str) -> List[str]:
        if is_connection_active(self.session_id):
            logger.info("[Resolver] Active Google login in progress, skipping Playwright fallback scrape.")
            return []
        session_dir = f"playwright_sessions/{self.session_id}"

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=session_dir,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                field_map, found, missing = await detect_field_map(page, form_url)
                
                project_info = field_map.get("project_name")
                if not project_info:
                    # Try scanning labels manually if mapping did not match
                    logger.warning("[Resolver Scraper] 'project_name' field not mapped. Scraping first dropdown/radio container instead.")
                    
                # Locate project field container
                label_to_find = project_info["label"] if project_info else "Project"
                container = page.locator('.Qr7Oae, div[role="listitem"], div[jsmodel]').filter(has_text=label_to_find).last
                try:
                    await container.wait_for(state="visible", timeout=8000)
                except:
                    # Fallback to general listbox if no match by label
                    container = page.locator('div[role="listbox"]').first
                    
                scraped = []
                # Handle dropdown/listbox options
                if await container.locator('div[role="listbox"]').count() > 0 or await container.locator('[jsname="LgbsSe"]').count() > 0:
                    dropdown = container.locator('div[role="listbox"], [jsname="LgbsSe"]').first
                    await dropdown.click()
                    await page.wait_for_timeout(1000)
                    
                    option_locators = page.locator('div[role="option"]')
                    count = await option_locators.count()
                    if count == 0:
                        # Fallback global search
                        option_locators = page.locator('.v37h1d div[role="option"], div[role="option"]')
                        count = await option_locators.count()
                        
                    for i in range(count):
                        opt_text = (await option_locators.nth(i).inner_text()).strip()
                        if opt_text and opt_text not in ("Choose", "Select", "— Choose —") and not opt_text.startswith("Choose"):
                            scraped.append(opt_text)
                
                # Handle radio options if no dropdown
                if not scraped and (await container.locator('div[role="radiogroup"]').count() > 0 or await container.locator('label').count() > 0):
                    radio_locators = container.locator('.freebirdFormviewerViewItemsRadioOption, label, div[role="radio"]')
                    count = await radio_locators.count()
                    for i in range(count):
                        opt_text = (await radio_locators.nth(i).inner_text()).strip()
                        if opt_text and opt_text not in scraped:
                            scraped.append(opt_text)
                            
                # Unique list preserving order
                seen = set()
                return [x for x in scraped if not (x in seen or seen.add(x))]
            except Exception as e:
                logger.error(f"[Resolver Scraper] Failed to scrape project options: {e}")
                return []
            finally:
                await context.close()

    async def resolve_projects(self, raw_records: List[Dict[str, Any]], form_url: str) -> Dict[str, Any]:
        """
        Processes raw parsed Excel records, extracts unique project names, matches them against the catalog,
        and returns a dict:
        {
            "resolved_records": List[Dict],      # raw_records with project_name updated to resolved names
            "resolutions": List[Dict]            # mapping information with confidence metrics
        }
        """
        # 1. Fetch catalog
        catalog = await self.get_project_catalog(form_url)
        if not catalog:
            # Fallback when catalog cannot be retrieved: keep project names intact with 100% confidence (unresolved)
            return {
                "resolved_records": raw_records,
                "resolutions": []
            }

        # 2. Extract unique project names from raw_records
        unique_inputs = {(r.get("project_name") or "").strip() for r in raw_records if r.get("project_name")}
        unique_inputs = {p for p in unique_inputs if p}

        # 3. Load global alias mappings from ProjectAlias table
        alias_res = await self.db.execute(select(ProjectAlias))
        aliases = {a.input_project: a.resolved_project for a in alias_res.scalars().all()}

        # 4. Resolve unique project names
        from rapidfuzz import fuzz
        resolutions = {}

        for inp in unique_inputs:
            # Check Learning Alias Dictionary first (case-insensitive lookup)
            inp_clean = inp.strip().lower()
            matched_alias = next((v for k, v in aliases.items() if k.strip().lower() == inp_clean), None)
            if matched_alias and matched_alias in catalog:
                resolutions[inp] = {
                    "input_project": inp,
                    "resolved_project": matched_alias,
                    "confidence": 100,
                    "status": "Auto-Accepted",
                    "match_type": "Learned Alias"
                }
                continue

            # Level 1: Exact Match
            exact_match = next((t for t in catalog if t.strip().lower() == inp_clean), None)
            if exact_match:
                resolutions[inp] = {
                    "input_project": inp,
                    "resolved_project": exact_match,
                    "confidence": 100,
                    "status": "Auto-Accepted",
                    "match_type": "Exact Match"
                }
                continue

            inp_norm = normalize_name(inp)
            inp_tokens = get_tokens(inp)

            # Check for multiple prefix/containment matches in catalog (ambiguity check)
            contained_targets = [
                t for t in catalog
                if len(inp_norm) >= 4 and inp_norm in normalize_name(t)
            ]
            token_contained_targets = [
                t for t in catalog
                if len(inp_tokens) >= 2 and all(tok in get_tokens(t) for tok in inp_tokens)
            ]
            ambiguous_candidates = list(dict.fromkeys(contained_targets + token_contained_targets))

            # Matching Pipeline for fuzzy/token scoring
            scored_targets = []
            for target in catalog:
                target_norm = normalize_name(target)
                target_tokens = get_tokens(target)

                # Normalized Match
                if inp_norm == target_norm:
                    scored_targets.append({
                        "target": target,
                        "score": 95,
                        "match_type": "Normalized Match"
                    })
                    continue

                # Token Overlap & Fuzzy Similarity
                matching_tokens = set(inp_tokens) & set(target_tokens)
                token_overlap_ratio = len(matching_tokens) / max(len(inp_tokens), 1)

                inp_numbers = [t for t in inp_tokens if t.isdigit()]
                num_penalty = 0
                if inp_numbers:
                    has_all_nums = all(num in target_norm for num in inp_numbers)
                    if not has_all_nums:
                        num_penalty = -40
                    else:
                        num_penalty = 20

                contained_tokens = [t for t in inp_tokens if t in target_norm]
                token_overlap_ratio = len(contained_tokens) / max(len(inp_tokens), 1)

                fuzzy_ratio = fuzz.ratio(inp_norm, target_norm)
                fuzzy_wratio = fuzz.WRatio(inp.lower(), target.lower())

                token_boost = 10 if token_overlap_ratio == 1.0 else 0

                score = (fuzzy_ratio * 0.35) + (fuzzy_wratio * 0.35) + (token_overlap_ratio * 20) + num_penalty + token_boost
                score = min(max(int(score), 0), 100)

                scored_targets.append({
                    "target": target,
                    "score": score,
                    "match_type": "Fuzzy / Weighted Match"
                })

            candidates = sorted(scored_targets, key=lambda x: x["score"], reverse=True)
            top = candidates[0] if candidates else None
            second = candidates[1] if len(candidates) > 1 else None

            # Detect ambiguity: multiple containment matches OR top two close high scores
            is_ambiguous = False
            if len(ambiguous_candidates) > 1:
                is_ambiguous = True
            elif top and second and top["score"] >= 75 and second["score"] >= 75 and (top["score"] - second["score"]) <= 6:
                is_ambiguous = True

            if is_ambiguous:
                resolutions[inp] = {
                    "input_project": inp,
                    "resolved_project": "",
                    "confidence": 0,
                    "status": "Needs Review",
                    "match_type": "Ambiguous Match (Manual Selection Required)"
                }
            elif top:
                if top["score"] >= 90:
                    status = "Auto-Accepted"
                elif top["score"] >= 80:
                    status = "Smart Match"
                else:
                    status = "Needs Review"

                resolutions[inp] = {
                    "input_project": inp,
                    "resolved_project": top["target"],
                    "confidence": top["score"],
                    "status": status,
                    "match_type": top["match_type"]
                }
            else:
                resolutions[inp] = {
                    "input_project": inp,
                    "resolved_project": inp,
                    "confidence": 0,
                    "status": "Needs Review",
                    "match_type": "No Catalog Match"
                }

        # 5. Apply resolutions to raw_records
        resolved_records = []
        for r in raw_records:
            rec_copy = dict(r)
            inp_proj = (r.get("project_name") or "").strip()
            if inp_proj in resolutions:
                res_info = resolutions[inp_proj]
                # Only automatically resolve if auto-accept or smart match (>= 80% confidence)
                # and resolved_project is non-empty
                if res_info["confidence"] >= 80 and res_info["resolved_project"]:
                    rec_copy["project_name"] = res_info["resolved_project"]
            resolved_records.append(rec_copy)

        return {
            "resolved_records": resolved_records,
            "resolutions": list(resolutions.values())
        }

def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex
