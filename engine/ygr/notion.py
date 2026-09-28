"""Writes Reddit leads to the existing Notion CRM (Companies + Contacts).

Uses the Notion API with an integration token (NOTION_TOKEN) that has been
shared with both databases. Without a token, or in dry run, it logs what it
would have written and returns None.

Field layout mirrors inquiry-agent: Company.Source = "Reddit" (option added
2026-09-25), Contact has no Source field so the provenance goes in Response
Notes. Never invents a select option: values are checked against the known
option lists and dropped if they don't match.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"


def _rt(text: str) -> list:
    return [{"type": "text", "text": {"content": text[:1900]}}]


class NotionIO:
    def __init__(self, cfg, store, env: dict | None = None):
        env = env or os.environ
        self.cfg = cfg
        self.store = store
        self.token = env.get("NOTION_TOKEN", "")
        n = cfg.base["notion"]
        self.companies = n["companies_ds"]
        self.contacts = n["contacts_ds"]
        self.segments = set(n["icp_segment_options"])
        self.region_map = n["region_map"]
        self.region_fallback = n["region_fallback"]

    @property
    def live(self) -> bool:
        return bool(self.token) and not self.cfg.dry_run

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{API}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Notion-Version": VERSION, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Notion {e.code}: {e.read()[:300]!r}") from e

    def _find_by_title(self, ds: str, name: str) -> str | None:
        res = self._req("POST", f"/data_sources/{ds}/query",
                        {"filter": {"property": "Name", "title": {"equals": name}}, "page_size": 1})
        rows = res.get("results", [])
        return rows[0]["id"] if rows else None

    def _create(self, ds: str, props: dict) -> str:
        res = self._req("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": ds}, "properties": props})
        return res["id"]

    def write_lead(self, *, handle: str, thread_url: str, asked: str, company: str = "", website: str = "",
                   country: str = "", segment: str = "", pain: str = "", contact_name: str = "") -> dict:
        region = self.region_map.get(country, self.region_fallback)
        segment = segment if segment in self.segments else ""
        name = contact_name or f"u/{handle}"
        plan = {"company": company, "contact": name, "region": region, "segment": segment}
        if not self.live:
            self.store.event("info", "notion", f"dry run: would write lead {name}", plan)
            return {"company_id": None, "contact_id": None, "dry_run": True, **plan}
        company_id = None
        if company:
            company_id = self._find_by_title(self.companies, company)
            if not company_id:
                props = {"Name": {"title": _rt(company)}, "Source": {"select": {"name": "Reddit"}},
                         "Region": {"select": {"name": region}}, "Account Status": {"select": {"name": "🌡️ Warm"}},
                         "Internal Notes": {"rich_text": _rt(f"Reddit: {thread_url} · u/{handle}")},
                         "Pain Points": {"rich_text": _rt(pain or asked)}}
                if website:
                    props["Website"] = {"url": website}
                company_id = self._create(self.companies, props)
        contact_id = self._find_by_title(self.contacts, name)
        if not contact_id:
            props = {"Name": {"title": _rt(name)},
                     "Response Notes": {"rich_text": _rt(f"Source: Reddit — {thread_url} — u/{handle} — {asked}")}}
            if segment:
                props["ICP Segment"] = {"select": {"name": segment}}
            if company_id:
                props["Company"] = {"relation": [{"id": company_id}]}
            contact_id = self._create(self.contacts, props)
        return {"company_id": company_id, "contact_id": contact_id, **plan}
