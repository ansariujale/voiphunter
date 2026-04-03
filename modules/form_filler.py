"""
WholesaleHunter v2 — Contact Form Filling Module
Uses Playwright (headless browser) to find and fill contact forms on lead websites.
"""

import re
import time
import logging
import asyncio
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

from config import (
    ROZPER, FORM_PATHS_TO_TRY, FORM_MESSAGE_TEMPLATE,
    FORM_FILL_DELAY_SECONDS, REQUEST_TIMEOUT,
)
from modules.database import update_lead, log_outreach

logger = logging.getLogger("wholesalehunter.formfiller")


# ═══════════════════════════════════════════════════════════════
# FORM DETECTION
# ═══════════════════════════════════════════════════════════════

async def find_contact_form(page: Page, base_url: str) -> Optional[str]:
    """
    Try to find a contact form on the website.
    Returns the URL of the page with the form, or None.
    """
    # First check the homepage for a contact form
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
        if await _page_has_form(page):
            return base_url
    except Exception as e:
        logger.debug(f"Homepage check failed for {base_url}: {e}")

    # Try common contact page paths
    for path in FORM_PATHS_TO_TRY:
        url = urljoin(base_url, path)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            if response and response.status == 200 and await _page_has_form(page):
                return url
        except Exception:
            continue

    # Try finding contact link in navigation
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
        contact_link = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a'));
            const contactLink = links.find(a => {
                const text = (a.textContent || '').toLowerCase();
                const href = (a.href || '').toLowerCase();
                return text.includes('contact') || text.includes('get in touch')
                    || text.includes('reach us') || text.includes('inquiry')
                    || href.includes('contact') || href.includes('inquiry');
            });
            return contactLink ? contactLink.href : null;
        }""")
        if contact_link:
            await page.goto(contact_link, wait_until="domcontentloaded", timeout=15000)
            if await _page_has_form(page):
                return contact_link
    except Exception:
        pass

    return None


async def _page_has_form(page: Page) -> bool:
    """Check if the current page has a contact/inquiry form."""
    try:
        has_form = await page.evaluate("""() => {
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const inputs = form.querySelectorAll('input, textarea, select');
                const hasEmail = Array.from(inputs).some(i =>
                    (i.type || '').includes('email') ||
                    (i.name || '').toLowerCase().includes('email') ||
                    (i.placeholder || '').toLowerCase().includes('email')
                );
                const hasMessage = Array.from(inputs).some(i =>
                    i.tagName === 'TEXTAREA' ||
                    (i.name || '').toLowerCase().includes('message') ||
                    (i.name || '').toLowerCase().includes('comment') ||
                    (i.name || '').toLowerCase().includes('inquiry')
                );
                // A contact form typically has email + message fields
                if (hasEmail && hasMessage && inputs.length >= 3) {
                    return true;
                }
            }
            return false;
        }""")
        return has_form
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# FORM FILLING
# ═══════════════════════════════════════════════════════════════

async def fill_contact_form(page: Page, lead: dict) -> bool:
    """
    Fill and submit a contact form on the current page.
    Returns True if submission was successful.
    """
    message = FORM_MESSAGE_TEMPLATE.format(
        contact_name=ROZPER["contact_name"],
        company_name=ROZPER["company_name"],
        coverage=ROZPER["coverage"],
    )

    # Customize message per lead
    country = lead.get("country", "")
    if country:
        message = message.replace(
            "your key destinations",
            f"your key destinations (we have strong routes to {country})"
        )

    try:
        filled = await page.evaluate("""(data) => {
            const forms = document.querySelectorAll('form');
            let targetForm = null;

            // Find the contact form
            for (const form of forms) {
                const inputs = form.querySelectorAll('input, textarea, select');
                const hasEmail = Array.from(inputs).some(i =>
                    (i.type || '').includes('email') ||
                    (i.name || '').toLowerCase().includes('email')
                );
                if (hasEmail && inputs.length >= 3) {
                    targetForm = form;
                    break;
                }
            }

            if (!targetForm) return { success: false, reason: 'No form found' };

            const inputs = targetForm.querySelectorAll('input, textarea, select');
            let filledFields = 0;

            for (const input of inputs) {
                const name = (input.name || '').toLowerCase();
                const type = (input.type || '').toLowerCase();
                const placeholder = (input.placeholder || '').toLowerCase();
                const label = input.labels?.[0]?.textContent?.toLowerCase() || '';
                const id = (input.id || '').toLowerCase();
                const combined = name + ' ' + placeholder + ' ' + label + ' ' + id;

                // Skip hidden, submit, checkbox fields
                if (type === 'hidden' || type === 'submit' || type === 'button' ||
                    type === 'checkbox' || type === 'radio' || type === 'file') continue;

                // Name field
                if (combined.includes('name') && !combined.includes('company') && !combined.includes('last')) {
                    input.value = data.name;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
                // First name
                else if (combined.includes('first')) {
                    input.value = data.firstName;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
                // Last name
                else if (combined.includes('last') || combined.includes('surname')) {
                    input.value = data.lastName;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
                // Email
                else if (type === 'email' || combined.includes('email')) {
                    input.value = data.email;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
                // Phone
                else if (type === 'tel' || combined.includes('phone') || combined.includes('tel')) {
                    input.value = data.phone || '';
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
                // Company
                else if (combined.includes('company') || combined.includes('organization') || combined.includes('org')) {
                    input.value = data.company;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
                // Subject
                else if (combined.includes('subject') || combined.includes('topic') || combined.includes('regarding')) {
                    input.value = data.subject;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
                // Message/textarea
                else if (input.tagName === 'TEXTAREA' || combined.includes('message') ||
                         combined.includes('comment') || combined.includes('inquiry') ||
                         combined.includes('details') || combined.includes('question')) {
                    input.value = data.message;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
                // Website/URL
                else if (type === 'url' || combined.includes('website') || combined.includes('url')) {
                    input.value = data.website;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    filledFields++;
                }
            }

            return { success: filledFields >= 3, filledFields: filledFields };
        }""", {
            "name": ROZPER["contact_name"],
            "firstName": ROZPER["contact_name"].split()[0],
            "lastName": ROZPER["contact_name"].split()[-1] if len(ROZPER["contact_name"].split()) > 1 else "",
            "email": ROZPER["contact_email"],
            "phone": "",
            "company": ROZPER["company_name"],
            "subject": f"Partnership Inquiry — Premium Voice Routes to {country}",
            "message": message,
            "website": ROZPER["website"],
        })

        if not filled.get("success"):
            logger.debug(f"Form fill incomplete: only {filled.get('filledFields', 0)} fields filled")
            return False

        # Small delay to look human
        await asyncio.sleep(1)

        # Submit the form
        submitted = await page.evaluate("""() => {
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const submitBtn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
                if (submitBtn) {
                    submitBtn.click();
                    return true;
                }
                // Try form.submit() as fallback
                form.submit();
                return true;
            }
            return false;
        }""")

        if submitted:
            # Wait for submission to complete
            await asyncio.sleep(3)
            logger.info(f"Form submitted successfully for {lead.get('company_domain')}")
            return True

        return False

    except Exception as e:
        logger.error(f"Form fill error for {lead.get('company_domain')}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# BATCH FORM FILLING
# ═══════════════════════════════════════════════════════════════

async def process_lead_form(browser: Browser, lead: dict) -> dict:
    """Process a single lead: find form, fill it, report result."""
    website = lead.get("website_url", "")
    if not website:
        return {"success": False, "reason": "no_website"}

    if not website.startswith("http"):
        website = f"https://{website}"

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        viewport={"width": 1280, "height": 720},
    )
    page = await context.new_page()

    try:
        # Find the contact form
        form_url = await find_contact_form(page, website)

        if not form_url:
            # No contact form found
            update_lead(lead["id"], {"has_contact_form": False})
            return {"success": False, "reason": "no_form"}

        # Navigate to the form page (if not already there)
        if page.url != form_url:
            await page.goto(form_url, wait_until="domcontentloaded", timeout=15000)

        # Fill and submit
        success = await fill_contact_form(page, lead)

        if success:
            update_lead(lead["id"], {
                "has_contact_form": True,
                "form_filled": True,
                "form_filled_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
            })
            log_outreach(
                lead_id=lead["id"],
                channel="form",
                form_url=form_url,
                form_submitted=True,
            )
            return {"success": True, "form_url": form_url}
        else:
            update_lead(lead["id"], {"has_contact_form": True})
            return {"success": False, "reason": "fill_failed"}

    except PlaywrightTimeout:
        return {"success": False, "reason": "timeout"}
    except Exception as e:
        logger.error(f"Form processing error for {lead.get('company_domain')}: {e}")
        return {"success": False, "reason": str(e)}
    finally:
        await context.close()


async def run_form_filling(leads: list[dict], max_concurrent: int = 3) -> dict:
    """
    Run form filling for a batch of leads.
    Uses Playwright with controlled concurrency.
    Returns summary stats.
    """
    stats = {"total": len(leads), "success": 0, "no_form": 0, "failed": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # Process in batches to control concurrency
        for i in range(0, len(leads), max_concurrent):
            batch = leads[i:i + max_concurrent]
            tasks = [process_lead_form(browser, lead) for lead in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    stats["failed"] += 1
                    logger.error(f"Form fill exception: {result}")
                elif result.get("success"):
                    stats["success"] += 1
                elif result.get("reason") == "no_form":
                    stats["no_form"] += 1
                else:
                    stats["failed"] += 1

            # Rate limiting between batches
            await asyncio.sleep(FORM_FILL_DELAY_SECONDS)

            if (i + max_concurrent) % 50 == 0:
                logger.info(f"Form filling progress: {i + max_concurrent}/{len(leads)} "
                            f"(success: {stats['success']}, no_form: {stats['no_form']})")

        await browser.close()

    logger.info(f"Form filling complete: {stats}")
    return stats


def fill_forms_sync(leads: list[dict], max_concurrent: int = 3) -> dict:
    """Synchronous wrapper for form filling."""
    return asyncio.run(run_form_filling(leads, max_concurrent))
