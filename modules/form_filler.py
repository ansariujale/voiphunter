"""
WholesaleHunter v2 — Contact Form Filling Module
Uses Playwright (headless browser) to find and fill contact forms on lead websites.
Human-like behavior with random delays, CAPTCHA detection, cookie banner dismissal.
"""

import re
import random
import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

from config import (
    FORM_FILL_DATA, FORM_PATHS_TO_TRY, FORM_FILL_DELAY_SECONDS, REQUEST_TIMEOUT,
)
from modules.database import update_lead, log_outreach

logger = logging.getLogger("wholesalehunter.formfiller")


# ═══════════════════════════════════════════════════════════════
# HUMAN-LIKE DELAYS
# ═══════════════════════════════════════════════════════════════

async def human_delay(min_sec: float = 0.5, max_sec: float = 2.0):
    """Random delay to mimic human typing/clicking."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def human_type(page: Page, selector: str, text: str):
    """Type text into a field with human-like speed."""
    try:
        await page.click(selector, timeout=3000)
        await human_delay(0.3, 0.8)
        await page.fill(selector, text, timeout=3000)
        await human_delay(0.2, 0.5)
    except Exception:
        # Fallback: try evaluate
        try:
            await page.evaluate(f"""(text) => {{
                const el = document.querySelector('{selector}');
                if (el) {{ el.value = text; el.dispatchEvent(new Event('input', {{bubbles: true}})); }}
            }}""", text)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# COOKIE BANNER DISMISSAL
# ═══════════════════════════════════════════════════════════════

async def dismiss_cookie_banner(page: Page):
    """Try to dismiss cookie consent banners."""
    cookie_selectors = [
        'button:has-text("Accept")',
        'button:has-text("Accept All")',
        'button:has-text("Accept Cookies")',
        'button:has-text("I Agree")',
        'button:has-text("OK")',
        'button:has-text("Got it")',
        'button:has-text("Dismiss")',
        'button:has-text("Close")',
        '[id*="cookie"] button',
        '[class*="cookie"] button',
        '[id*="consent"] button',
        '[class*="consent"] button',
        '[data-testid*="cookie"] button',
    ]
    for selector in cookie_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click(timeout=2000)
                await human_delay(0.5, 1.0)
                return True
        except Exception:
            continue
    return False


# ═══════════════════════════════════════════════════════════════
# CAPTCHA DETECTION
# ═══════════════════════════════════════════════════════════════

async def has_captcha(page: Page) -> bool:
    """Check if the page has a CAPTCHA that would block form submission."""
    try:
        has = await page.evaluate("""() => {
            const html = document.documentElement.innerHTML.toLowerCase();
            // Check for reCAPTCHA
            if (document.querySelector('iframe[src*="recaptcha"]')) return true;
            if (document.querySelector('.g-recaptcha')) return true;
            // Check for hCaptcha
            if (document.querySelector('iframe[src*="hcaptcha"]')) return true;
            if (document.querySelector('.h-captcha')) return true;
            // Check for Cloudflare Turnstile
            if (document.querySelector('iframe[src*="turnstile"]')) return true;
            if (document.querySelector('.cf-turnstile')) return true;
            return false;
        }""")
        return has
    except Exception:
        return False


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
        await human_delay(1.0, 2.0)
        await dismiss_cookie_banner(page)
        if await _page_has_form(page):
            return base_url
    except Exception as e:
        logger.debug(f"Homepage check failed for {base_url}: {e}")

    # Try common contact page paths
    for path in FORM_PATHS_TO_TRY:
        url = urljoin(base_url, path)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            if response and response.status == 200:
                await human_delay(0.5, 1.5)
                if await _page_has_form(page):
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
                    || text.includes('enquiry') || text.includes('request')
                    || href.includes('contact') || href.includes('inquiry');
            });
            return contactLink ? contactLink.href : null;
        }""")
        if contact_link:
            await page.goto(contact_link, wait_until="domcontentloaded", timeout=15000)
            await human_delay(0.5, 1.0)
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
                    (i.name || '').toLowerCase().includes('inquiry') ||
                    (i.name || '').toLowerCase().includes('details')
                );
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
# FORM FILLING (Human-like with native Playwright calls)
# ═══════════════════════════════════════════════════════════════

async def fill_contact_form(page: Page, lead: dict) -> dict:
    """
    Fill and submit a contact form on the current page.
    Returns dict with: success, fields_filled, error_message
    """
    data = FORM_FILL_DATA

    # Check for CAPTCHA first
    if await has_captcha(page):
        return {"success": False, "fields_filled": 0, "error_message": "CAPTCHA detected — skipped"}

    try:
        # Map form fields using JavaScript analysis
        field_map = await page.evaluate("""(data) => {
            const forms = document.querySelectorAll('form');
            let targetForm = null;

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

            if (!targetForm) return { found: false };

            const inputs = targetForm.querySelectorAll('input, textarea, select');
            const fields = [];

            for (const input of inputs) {
                const name = (input.name || '').toLowerCase();
                const type = (input.type || '').toLowerCase();
                const placeholder = (input.placeholder || '').toLowerCase();
                const label = input.labels?.[0]?.textContent?.toLowerCase() || '';
                const id = (input.id || '').toLowerCase();
                const combined = name + ' ' + placeholder + ' ' + label + ' ' + id;
                const ariaLabel = (input.getAttribute('aria-label') || '').toLowerCase();
                const allText = combined + ' ' + ariaLabel;

                if (type === 'hidden' || type === 'submit' || type === 'button' ||
                    type === 'checkbox' || type === 'radio' || type === 'file') continue;

                // Build a unique CSS selector for this element
                let selector = '';
                if (input.id) selector = '#' + CSS.escape(input.id);
                else if (input.name) selector = `[name="${CSS.escape(input.name)}"]`;
                else {
                    const idx = Array.from(targetForm.querySelectorAll(input.tagName)).indexOf(input);
                    selector = `form ${input.tagName.toLowerCase()}:nth-of-type(${idx + 1})`;
                }

                let fieldType = null;
                // First name
                if (allText.includes('first') && allText.includes('name')) fieldType = 'first_name';
                // Last name
                else if (allText.includes('last') || allText.includes('surname') || allText.includes('family')) fieldType = 'last_name';
                // Full name (but not company name)
                else if ((allText.includes('name') || allText.includes('full name') || allText.includes('your name'))
                    && !allText.includes('company') && !allText.includes('org') && !allText.includes('business')
                    && !allText.includes('last') && !allText.includes('first')) fieldType = 'name';
                // Email
                else if (type === 'email' || allText.includes('email') || allText.includes('e-mail')) fieldType = 'email';
                // Phone
                else if (type === 'tel' || allText.includes('phone') || allText.includes('tel') || allText.includes('mobile') || allText.includes('cell')) fieldType = 'phone';
                // Company
                else if (allText.includes('company') || allText.includes('organization') || allText.includes('organisation') || allText.includes('business name') || allText.includes('org name')) fieldType = 'company';
                // Subject
                else if (allText.includes('subject') || allText.includes('topic') || allText.includes('regarding') || allText.includes('reason')) fieldType = 'subject';
                // Message (textarea or message field)
                else if (input.tagName === 'TEXTAREA' || allText.includes('message') || allText.includes('comment')
                    || allText.includes('inquiry') || allText.includes('enquiry') || allText.includes('details')
                    || allText.includes('question') || allText.includes('description') || allText.includes('how can we help')) fieldType = 'message';
                // Website/URL
                else if (type === 'url' || allText.includes('website') || allText.includes('url')) fieldType = 'website';

                if (fieldType) {
                    fields.push({ selector, fieldType, tagName: input.tagName });
                }
            }

            return { found: true, fields };
        }""", data)

        if not field_map.get("found"):
            return {"success": False, "fields_filled": 0, "error_message": "No contact form found on page"}

        fields = field_map.get("fields", [])
        if len(fields) < 2:
            return {"success": False, "fields_filled": 0, "error_message": "Too few fillable fields detected"}

        # Fill each field with human-like delays
        filled_count = 0
        value_map = {
            'name': data['name'],
            'first_name': data['first_name'],
            'last_name': data['last_name'],
            'email': data['email'],
            'phone': data['phone'],
            'company': data['company'],
            'subject': data['subject'],
            'message': data['message'],
            'website': 'https://rozper.com',
        }

        for field in fields:
            field_type = field["fieldType"]
            selector = field["selector"]
            value = value_map.get(field_type, "")
            if not value:
                continue

            try:
                await human_delay(0.5, 1.5)
                # Try native Playwright fill first
                try:
                    await page.click(selector, timeout=3000)
                    await human_delay(0.2, 0.5)
                    await page.fill(selector, value, timeout=3000)
                except Exception:
                    # Fallback: use evaluate
                    await page.evaluate("""({sel, val}) => {
                        const el = document.querySelector(sel);
                        if (el) {
                            el.focus();
                            el.value = val;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""", {"sel": selector, "val": value})
                filled_count += 1
            except Exception as e:
                logger.debug(f"Failed to fill {field_type} ({selector}): {e}")

        if filled_count < 2:
            return {"success": False, "fields_filled": filled_count, "error_message": f"Only filled {filled_count} fields (need at least 2)"}

        # Human pause before submitting
        await human_delay(1.0, 3.0)

        # Capture URL before submit for change detection
        url_before = page.url

        # Submit the form
        submitted = await page.evaluate("""() => {
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const inputs = form.querySelectorAll('input, textarea');
                const hasEmail = Array.from(inputs).some(i =>
                    (i.type || '').includes('email') || (i.name || '').toLowerCase().includes('email'));
                if (!hasEmail || inputs.length < 3) continue;

                // Try clicking submit button
                const submitBtn = form.querySelector(
                    'button[type="submit"], input[type="submit"], ' +
                    'button:not([type]), button[type="button"]'
                );
                if (submitBtn) {
                    submitBtn.click();
                    return 'clicked';
                }
                // Fallback: form.submit()
                try { form.submit(); return 'submitted'; } catch(e) {}
            }
            // Try any visible submit-like button on page
            const btns = document.querySelectorAll('button, input[type="submit"]');
            for (const btn of btns) {
                const text = (btn.textContent || btn.value || '').toLowerCase();
                if (text.includes('submit') || text.includes('send') || text.includes('contact')
                    || text.includes('get in touch') || text.includes('request')) {
                    btn.click();
                    return 'clicked_fallback';
                }
            }
            return 'no_button';
        }""")

        if submitted == "no_button":
            return {"success": False, "fields_filled": filled_count, "error_message": "No submit button found"}

        # Wait for submission to process
        await asyncio.sleep(3)

        # Check for success indicators
        success = await _detect_submission_success(page, url_before)

        if success:
            logger.info(f"Form submitted successfully for {lead.get('company_domain', lead.get('company_name', 'unknown'))}")
            return {"success": True, "fields_filled": filled_count, "error_message": None}
        else:
            # Check for visible errors
            error_text = await _detect_form_errors(page)
            return {
                "success": False,
                "fields_filled": filled_count,
                "error_message": error_text or "Submission may have failed (no success indicator)"
            }

    except Exception as e:
        logger.error(f"Form fill error for {lead.get('company_domain', 'unknown')}: {e}")
        return {"success": False, "fields_filled": 0, "error_message": str(e)[:200]}


async def _detect_submission_success(page: Page, url_before: str) -> bool:
    """Check if the form was submitted successfully."""
    try:
        # Check for URL change (often redirects to thank-you page)
        if page.url != url_before and ('thank' in page.url.lower() or 'success' in page.url.lower()):
            return True

        # Check page content for success indicators
        success = await page.evaluate("""() => {
            const text = document.body.innerText.toLowerCase();
            const successPhrases = [
                'thank you', 'thanks for', 'message sent', 'form submitted',
                'successfully', 'we will get back', 'we\'ll get back',
                'received your', 'submission received', 'message received',
                'request received', 'inquiry received', 'we\'ll be in touch',
                'we will be in touch', 'confirmation', 'been submitted',
                'appreciate your', 'hear from us'
            ];
            return successPhrases.some(phrase => text.includes(phrase));
        }""")
        return success
    except Exception:
        # If page navigated away, likely success
        return True


async def _detect_form_errors(page: Page) -> Optional[str]:
    """Check for visible error messages on the form."""
    try:
        error = await page.evaluate("""() => {
            const errorSelectors = [
                '.error', '.form-error', '.field-error', '.alert-danger',
                '.validation-error', '[role="alert"]', '.error-message',
                '.form-message.error', '.wpcf7-not-valid-tip'
            ];
            for (const sel of errorSelectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) {
                    return el.textContent.trim().substring(0, 200);
                }
            }
            return null;
        }""")
        return error
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# BATCH FORM FILLING
# ═══════════════════════════════════════════════════════════════

async def process_lead_form(browser: Browser, lead: dict) -> dict:
    """Process a single lead: find form, fill it, report result."""
    website = lead.get("website_url", "")
    if not website:
        return {"lead_id": lead.get("id"), "success": False, "reason": "no_website",
                "error_message": "No website URL", "form_url": None, "fields_filled": 0}

    if not website.startswith("http"):
        website = f"https://{website}"

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 720},
    )
    page = await context.new_page()

    try:
        # Find the contact form
        form_url = await find_contact_form(page, website)

        if not form_url:
            now = datetime.now(timezone.utc).isoformat()
            update_lead(lead["id"], {
                "has_contact_form": False,
                "form_submission_status": "failed",
                "form_error_message": "No contact form found",
                "form_last_attempted_at": now,
            })
            return {"lead_id": lead["id"], "success": False, "reason": "no_form",
                    "error_message": "No contact form found", "form_url": None, "fields_filled": 0}

        # Navigate to the form page (if not already there)
        if page.url != form_url:
            await page.goto(form_url, wait_until="domcontentloaded", timeout=15000)
            await human_delay(0.5, 1.5)

        # Fill and submit
        result = await fill_contact_form(page, lead)
        now = datetime.now(timezone.utc).isoformat()

        if result["success"]:
            update_lead(lead["id"], {
                "has_contact_form": True,
                "form_filled": True,
                "form_filled_at": now,
                "contact_page_url": form_url,
                "form_submission_status": "success",
                "form_error_message": None,
                "form_last_attempted_at": now,
            })
            log_outreach(
                lead_id=lead["id"],
                channel="form",
                form_url=form_url,
                form_submitted=True,
            )
            return {"lead_id": lead["id"], "success": True, "form_url": form_url,
                    "fields_filled": result["fields_filled"], "error_message": None, "reason": "success"}
        else:
            update_lead(lead["id"], {
                "has_contact_form": True,
                "contact_page_url": form_url,
                "form_submission_status": "failed",
                "form_error_message": result.get("error_message", "Fill failed"),
                "form_last_attempted_at": now,
            })
            return {"lead_id": lead["id"], "success": False, "reason": "fill_failed",
                    "error_message": result.get("error_message", "Fill failed"),
                    "form_url": form_url, "fields_filled": result["fields_filled"]}

    except PlaywrightTimeout:
        now = datetime.now(timezone.utc).isoformat()
        update_lead(lead["id"], {
            "form_submission_status": "failed",
            "form_error_message": "Page load timeout",
            "form_last_attempted_at": now,
        })
        return {"lead_id": lead["id"], "success": False, "reason": "timeout",
                "error_message": "Page load timeout", "form_url": None, "fields_filled": 0}
    except Exception as e:
        now = datetime.now(timezone.utc).isoformat()
        error_msg = str(e)[:200]
        update_lead(lead["id"], {
            "form_submission_status": "failed",
            "form_error_message": error_msg,
            "form_last_attempted_at": now,
        })
        logger.error(f"Form processing error for {lead.get('company_domain')}: {e}")
        return {"lead_id": lead["id"], "success": False, "reason": "error",
                "error_message": error_msg, "form_url": None, "fields_filled": 0}
    finally:
        await context.close()


async def run_form_filling(leads: list[dict], max_concurrent: int = 3) -> dict:
    """
    Run form filling for a batch of leads.
    Uses Playwright with controlled concurrency.
    Returns summary stats.
    """
    stats = {"total": len(leads), "success": 0, "no_form": 0, "failed": 0, "results": []}

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
                    stats["results"].append({"success": False, "error_message": str(result)[:200]})
                    logger.error(f"Form fill exception: {result}")
                elif result.get("success"):
                    stats["success"] += 1
                    stats["results"].append(result)
                elif result.get("reason") == "no_form":
                    stats["no_form"] += 1
                    stats["results"].append(result)
                else:
                    stats["failed"] += 1
                    stats["results"].append(result)

            # Rate limiting between batches
            await asyncio.sleep(FORM_FILL_DELAY_SECONDS)

            if (i + max_concurrent) % 50 == 0:
                logger.info(f"Form filling progress: {i + max_concurrent}/{len(leads)} "
                            f"(success: {stats['success']}, no_form: {stats['no_form']})")

        await browser.close()

    logger.info(f"Form filling complete: total={stats['total']}, success={stats['success']}, "
                f"no_form={stats['no_form']}, failed={stats['failed']}")
    return stats


def fill_forms_sync(leads: list[dict], max_concurrent: int = 3) -> dict:
    """Synchronous wrapper for form filling."""
    return asyncio.run(run_form_filling(leads, max_concurrent))
