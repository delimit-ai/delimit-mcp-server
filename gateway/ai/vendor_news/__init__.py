"""Vendor-news riff system (LED-1250).

Sensor + drafter that detects high-engagement vendor announcements on X
and auto-drafts a brand-voice Delimit-POV riff that rides the news cycle
for algorithm boost.

Public surface:
    from ai.vendor_news import scan_vendor_news, draft_vendor_riff
"""

from ai.vendor_news.sensor import scan_vendor_news
from ai.vendor_news.drafter import draft_vendor_riff

__all__ = ["scan_vendor_news", "draft_vendor_riff"]
