# Demographics Research Agent

Install dependencies and the browser used for fallback fetching:

```sh
uv sync
uv run playwright install chromium
uv run python main.py
```

Pages are fetched with Requests first. Request errors, empty pages, and common
access challenges trigger a headless Playwright/Chromium retry. Both paths extract
text from the retrieved HTML for the existing summarisation and UN comparison.
PDF URLs are read directly with Requests and `pypdf`, so their extracted document
text follows the same summarisation and UN comparison flow.
If both fail, the analysis stops and displays the error. The Debug status field
shows the current fetch method while the analysis runs.

Run the regression checks with `uv run python -m unittest discover -s tests`.
