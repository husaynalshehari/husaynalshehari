"""playwright.sync_api, as far as the groups script uses it."""
from ._impl import (BrowserContext, CDPSession, Error, Keyboard, Locator, Mouse, Page, Playwright, Request, Response,
                    Route, TimeoutError, sync_playwright)

__all__ = ["BrowserContext", "CDPSession", "Error", "Keyboard", "Locator", "Mouse", "Page", "Playwright", "Request",
           "Response", "Route", "TimeoutError", "sync_playwright"]
