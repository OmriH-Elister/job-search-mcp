import logging
import smtplib
import uuid
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Optional
from modules.browser_manager import BrowserManager

logger = logging.getLogger(__name__)

# ── Cover Letter Templates ────────────────────────────────────────────────────

COVER_LETTER_TEMPLATE = """Dear Hiring Manager,

I am writing to express my interest in the {job_title} position at {company_name}.

{body_paragraph}

I would welcome the opportunity to discuss how my skills and experience can contribute to your team. I am available for an interview at your earliest convenience.

Best regards,
{candidate_name}
Phone: {candidate_phone}
Email: {candidate_email}
LinkedIn: {candidate_linkedin}"""

ROLE_PARAGRAPHS = {
    "SOC Analyst": (
        "With hands-on experience in security operations, SIEM monitoring, and incident "
        "triage, I bring a sharp eye for detecting and responding to threats in real time. "
        "My CEH certification and practical lab work in penetration testing give me a deep "
        "understanding of attacker tactics, enabling faster and more effective analysis."
    ),
    "Cyber Security Specialist": (
        "My background spans vulnerability assessment, penetration testing, and security "
        "architecture review. I hold a CEH certification and have practical experience with "
        "tools like Metasploit, Nmap, and Burp Suite. I am passionate about hardening systems "
        "and building defense-in-depth strategies."
    ),
    "SecOps Specialist": (
        "I combine security expertise with operational know-how — from configuring firewalls "
        "and IDS/IPS systems to automating security workflows with scripting. My approach bridges "
        "the gap between security policy and day-to-day IT operations."
    ),
    "IT Operations Specialist": (
        "I have a strong foundation in systems administration, infrastructure management, and "
        "IT service delivery. From Active Directory and Group Policy to virtualization and "
        "monitoring, I ensure reliable operations and rapid incident resolution."
    ),
    "Support Specialist": (
        "I thrive in technical support environments where clear communication meets deep "
        "troubleshooting skills. I have experience supporting enterprise users across Windows, "
        "Linux, and cloud platforms, with a focus on first-call resolution and user satisfaction."
    ),
    "Linux System Administrator": (
        "Linux is my home ground — from RHEL and Ubuntu server administration to shell scripting, "
        "service management, and performance tuning. I have experience managing production Linux "
        "environments and automating routine tasks with Bash and Python."
    ),
    "Monitoring Specialist": (
        "I specialize in building and maintaining monitoring and observability stacks — Zabbix, "
        "Grafana, Prometheus, ELK — ensuring 24/7 visibility into infrastructure health. I excel "
        "at alert tuning, dashboard design, and escalation workflows."
    ),
    "Penetration Tester": (
        "With a CEH certification and hands-on experience in offensive security, I conduct "
        "thorough penetration tests across web applications, networks, and infrastructure. "
        "I use industry-standard tools and methodologies to identify vulnerabilities before "
        "attackers do."
    ),
}


# ── Application Manager ──────────────────────────────────────────────────────

class ApplicationManager:
    def __init__(self, config: dict, browser: BrowserManager):
        self.config = config
        self.candidate = config["candidate"]
        self.cv_base_path = Path("/cvs") if os.path.exists("/cvs") else Path(config.get("cv_base_path", "."))
        self.browser = browser
        self._pending = {}

    def generate_job_id(self):
        return str(uuid.uuid4())[:8]

    def draft_cover_letter(self, job: dict) -> str:
        role = job.get("matched_role", "the position")
        body = ROLE_PARAGRAPHS.get(role, ROLE_PARAGRAPHS["IT Operations Specialist"])
        return COVER_LETTER_TEMPLATE.format(
            job_title=role,
            company_name=job.get("company", "your company"),
            body_paragraph=body,
            candidate_name=self.candidate["name"],
            candidate_phone=self.candidate["phone"],
            candidate_email=self.candidate["email"],
            candidate_linkedin=self.candidate["linkedin"],
        ).strip()

    def queue_for_approval(self, job: dict) -> str:
        job_id = self.generate_job_id()
        job.update({
            "job_id": job_id,
            "cover_letter": self.draft_cover_letter(job),
            "status": "pending_approval",
        })
        self._pending[job_id] = job
        return job_id

    def get_pending_approvals(self):
        return list(self._pending.values())

    # ── Main submission router ────────────────────────────────────────────

    async def submit(self, job_id: str) -> dict:
        if job_id not in self._pending:
            return {"success": False, "method": "none", "message": "Not found"}

        job = self._pending[job_id]
        cv_path = self.cv_base_path / job["cv_file"]

        if not cv_path.exists():
            return {"success": False, "method": "none",
                    "message": f"CV not found at {cv_path}"}

        ats_type = job.get("ats_type")
        result = None

        # ── Step 1: Try ATS-specific submission ──
        if ats_type == "greenhouse":
            result = await self._submit_greenhouse(job, cv_path)
        elif ats_type == "comeet":
            result = await self._submit_comeet(job, cv_path)
        elif ats_type == "lever":
            result = await self._submit_lever(job, cv_path)
        else:
            # Generic web form attempt
            result = await self._submit_web_form(job, cv_path)

        if result and result.get("success"):
            result["method"] = f"ats:{ats_type}" if ats_type else "web_form"
            return result

        # ── Step 2: ATS failed — prepare email fallback ──
        logger.info("ATS submission failed for %s, preparing email fallback", job_id)
        return {
            "success": False,
            "method": "needs_email_fallback",
            "message": f"ATS submission failed ({result.get('message', 'unknown')}). Email fallback needed.",
            "email_fallback": self._prepare_email_fallback(job, cv_path),
        }

    def _prepare_email_fallback(self, job: dict, cv_path: Path) -> dict:
        """Prepare all the details Claude needs to send via Gmail MCP."""
        from modules.careers_scanner import CareersScanner

        # Try found email first, then constructed candidates
        found_email = job.get("hr_email")
        company_url = job.get("company_url", job.get("url", ""))
        constructed = CareersScanner.construct_email_candidates(company_url)

        subject_template = self.config.get("application_methods", {}).get(
            "email_subject_template",
            "Application for {job_title} – {candidate_name}"
        )
        subject = subject_template.format(
            job_title=job.get("matched_role", "Open Position"),
            candidate_name=self.candidate["name"],
        )

        return {
            "to_email": found_email,
            "constructed_emails": constructed,
            "subject": subject,
            "body": job.get("cover_letter", ""),
            "cv_path": str(cv_path),
            "cv_filename": cv_path.name,
            "from_name": self.candidate["name"],
            "from_email": self.candidate["email"],
        }

    # ── ATS: Greenhouse ──────────────────────────────────────────────────

    async def _submit_greenhouse(self, job: dict, cv_path: Path) -> dict:
        page = await self.browser.new_page()
        try:
            url = job["url"]
            if not await self.browser.navigate(page, url):
                return {"success": False, "message": "Greenhouse page failed to load"}

            # Greenhouse forms have standard field IDs
            field_map = {
                "#first_name": self.candidate["name"].split()[0],
                "#last_name": " ".join(self.candidate["name"].split()[1:]),
                "#email": self.candidate["email"],
                "#phone": self.candidate["phone"],
            }

            for selector, value in field_map.items():
                try:
                    field = await page.query_selector(selector)
                    if field:
                        await field.fill(value)
                except Exception:
                    # Try alternative selectors
                    pass

            # Try common Greenhouse field patterns
            alt_fields = [
                ('input[name*="first_name"]', self.candidate["name"].split()[0]),
                ('input[name*="last_name"]', " ".join(self.candidate["name"].split()[1:])),
                ('input[name*="email"]', self.candidate["email"]),
                ('input[name*="phone"]', self.candidate["phone"]),
            ]
            for selector, value in alt_fields:
                try:
                    field = await page.query_selector(selector)
                    if field:
                        await field.fill(value)
                except Exception:
                    pass

            # Upload CV
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(str(cv_path))

            # Fill cover letter textarea
            try:
                cover = await page.query_selector('textarea[name*="cover_letter"], textarea[id*="cover_letter"]')
                if cover:
                    await cover.fill(job.get("cover_letter", ""))
            except Exception:
                pass

            # Submit
            import asyncio
            await asyncio.sleep(1)
            for selector in [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Submit")',
                'button:has-text("Apply")',
                'button:has-text("הגש")',
            ]:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(2)
                        return {"success": True, "message": "Submitted via Greenhouse ATS"}
                except Exception:
                    continue

            return {"success": False, "message": "Greenhouse: could not find submit button"}
        except Exception as e:
            logger.error("Greenhouse submission error: %s", e)
            return {"success": False, "message": f"Greenhouse error: {e}"}
        finally:
            await page.close()

    # ── ATS: Comeet ───────────────────────────────────────────────────────

    async def _submit_comeet(self, job: dict, cv_path: Path) -> dict:
        page = await self.browser.new_page()
        try:
            url = job["url"]
            if not await self.browser.navigate(page, url):
                return {"success": False, "message": "Comeet page failed to load"}

            # Click Apply button first to open the form
            import asyncio
            for btn_text in ["Apply", "Apply Now", "הגש מועמדות"]:
                if await self.browser.click_button(page, btn_text):
                    await asyncio.sleep(2)
                    break

            # Fill standard fields
            field_pairs = [
                ('input[name*="name"], input[placeholder*="name"], input[placeholder*="Name"]',
                 self.candidate["name"]),
                ('input[name*="email"], input[type="email"], input[placeholder*="email"]',
                 self.candidate["email"]),
                ('input[name*="phone"], input[type="tel"], input[placeholder*="phone"]',
                 self.candidate["phone"]),
            ]
            for selector, value in field_pairs:
                try:
                    field = await page.query_selector(selector)
                    if field:
                        await field.fill(value)
                except Exception:
                    pass

            # Upload CV
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(str(cv_path))

            # Submit
            await asyncio.sleep(1)
            for selector in [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Submit")',
                'button:has-text("Apply")',
                'button:has-text("Send")',
                'button:has-text("שלח")',
            ]:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(2)
                        return {"success": True, "message": "Submitted via Comeet ATS"}
                except Exception:
                    continue

            return {"success": False, "message": "Comeet: could not find submit button"}
        except Exception as e:
            logger.error("Comeet submission error: %s", e)
            return {"success": False, "message": f"Comeet error: {e}"}
        finally:
            await page.close()

    # ── ATS: Lever ────────────────────────────────────────────────────────

    async def _submit_lever(self, job: dict, cv_path: Path) -> dict:
        page = await self.browser.new_page()
        try:
            url = job["url"]
            if not await self.browser.navigate(page, url):
                return {"success": False, "message": "Lever page failed to load"}

            # Click Apply button
            import asyncio
            for btn_text in ["Apply", "Apply for this job"]:
                if await self.browser.click_button(page, btn_text):
                    await asyncio.sleep(2)
                    break

            # Lever uses specific field names
            field_pairs = [
                ('input[name="name"]', self.candidate["name"]),
                ('input[name="email"]', self.candidate["email"]),
                ('input[name="phone"]', self.candidate["phone"]),
                ('input[name="urls[LinkedIn]"]', self.candidate["linkedin"]),
            ]
            for selector, value in field_pairs:
                try:
                    field = await page.query_selector(selector)
                    if field:
                        await field.fill(value)
                except Exception:
                    pass

            # Upload CV
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(str(cv_path))

            # Cover letter
            try:
                cover = await page.query_selector('textarea[name="comments"]')
                if cover:
                    await cover.fill(job.get("cover_letter", ""))
            except Exception:
                pass

            # Submit
            await asyncio.sleep(1)
            for selector in [
                'button[type="submit"]',
                'button:has-text("Submit application")',
                'button:has-text("Submit")',
            ]:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(2)
                        return {"success": True, "message": "Submitted via Lever ATS"}
                except Exception:
                    continue

            return {"success": False, "message": "Lever: could not find submit button"}
        except Exception as e:
            logger.error("Lever submission error: %s", e)
            return {"success": False, "message": f"Lever error: {e}"}
        finally:
            await page.close()

    # ── Generic web form ──────────────────────────────────────────────────

    async def _submit_web_form(self, job: dict, cv_path: Path) -> dict:
        page = await self.browser.new_page()
        try:
            if not await self.browser.navigate(page, job["url"]):
                return {"success": False, "message": "Page load failed"}

            # Try to fill any visible form fields
            import asyncio
            field_pairs = [
                ('input[name*="name"], input[placeholder*="name"]', self.candidate["name"]),
                ('input[name*="email"], input[type="email"]', self.candidate["email"]),
                ('input[name*="phone"], input[type="tel"]', self.candidate["phone"]),
            ]
            for selector, value in field_pairs:
                try:
                    field = await page.query_selector(selector)
                    if field:
                        await field.fill(value)
                except Exception:
                    pass

            file_input = await self.browser.find_file_input(page)
            if file_input:
                await file_input.set_input_files(str(cv_path))

            for btn in ["Submit", "Apply", "Send", "הגש", "שלח"]:
                if await self.browser.click_button(page, btn):
                    return {"success": True, "message": "Submitted via web form"}
            return {"success": False, "message": "Submit button not found"}
        finally:
            await page.close()

    # ── SMTP email (if configured) ────────────────────────────────────────

    async def _submit_email(self, job: dict, cv_path: Path, to_addr: str) -> dict:
        try:
            smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
            smtp_port = int(os.getenv("SMTP_PORT", "587"))
            smtp_user = os.getenv("SMTP_USER", self.candidate["email"])
            smtp_pass = os.getenv("SMTP_PASS", "")

            if not smtp_pass:
                return {"success": False, "message": "SMTP_PASS not configured"}

            subject_template = self.config.get("application_methods", {}).get(
                "email_subject_template",
                "Application for {job_title} – {candidate_name}"
            )
            subject = subject_template.format(
                job_title=job.get("matched_role", "Open Position"),
                candidate_name=self.candidate["name"],
            )

            msg = MIMEMultipart()
            msg["From"] = smtp_user
            msg["To"] = to_addr
            msg["Subject"] = subject
            msg.attach(MIMEText(job.get("cover_letter", ""), "plain", "utf-8"))

            with open(cv_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={cv_path.name}")
                msg.attach(part)

            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, to_addr, msg.as_string())

            return {"success": True, "message": f"Email sent to {to_addr}"}
        except Exception as e:
            logger.error("Email send failed: %s", e)
            return {"success": False, "message": f"Email failed: {e}"}
